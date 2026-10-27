from collections import defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from django.conf import settings

from mindcare.api.common import consent, decrypt
from mindcare.models import (
    AnalysisReport,
    AnalysisRun,
    AnalysisSource,
    CheckIn,
    ChatMessage,
    JournalEntry,
)
from mindcare.services.configuration import get_analysis_policy, get_configured_callable


class DeepAnalysisService:
    """Build evidence-backed reports using a configured context-analysis adapter.

    ``analyzer`` is an adapter accepting the context dictionary and returning a
    report payload. It can be injected per call or configured in Django settings.
    """

    @classmethod
    @transaction.atomic
    def run(cls, user, trigger, analyzer=None, model_version='', now=None, run_id=None):
        now = now or timezone.now()
        policy = get_analysis_policy()
        existing = AnalysisRun.objects.select_for_update().get(id=run_id, user=user) if run_id else None
        start = existing.period_start if existing else now - timedelta(days=policy.deep_window_days)
        end = existing.period_end if existing else now
        if not any(consent(user, scope) for scope in ('ai_checkin', 'ai_journal', 'ai_chat')):
            raise PermissionError('Analysis requires a current source consent.')
        checkins = list(CheckIn.objects.filter(
            user=user, recorded_at__gte=start, recorded_at__lt=end
        ).order_by('recorded_at')) if consent(user, 'ai_checkin') else []
        journals = list(JournalEntry.objects.filter(
            user=user, experienced_at__gte=start, experienced_at__lt=end
        ).order_by('experienced_at')) if consent(user, 'ai_journal') else []
        messages = list(ChatMessage.objects.filter(
            session__user=user, created_at__gte=start, created_at__lt=end
        ).order_by('created_at')) if consent(user, 'ai_chat') else []

        missing_fields = []
        if not checkins:
            missing_fields.append(f'check_ins:last_{policy.deep_window_days}_days')
        if len(checkins) < policy.deep_minimum_checkins:
            missing_fields.append(f'check_ins:{policy.deep_minimum_checkins}_or_more_for_trend')
        context = cls._context(checkins, journals, messages, timezone_name=user.timezone)
        analyzer = analyzer or get_configured_callable('MINDCARE_DEEP_ANALYZER')
        report_data = analyzer(context)
        if not isinstance(report_data, dict):
            raise ValueError('Deep analysis adapter must return a dictionary.')
        required_report_fields = {'emotion_summary', 'concern_level', 'summary', 'recommendations'}
        missing_report_fields = required_report_fields - report_data.keys()
        if missing_report_fields:
            raise ValueError(
                'Deep analysis adapter response is missing: '
                + ', '.join(sorted(missing_report_fields))
            )

        run_fields = dict(
            status='completed',
            sufficiency_status='sufficient' if len(checkins) >= policy.deep_minimum_checkins else 'insufficient',
            missing_fields=missing_fields,
            data_quality={
                'check_ins': len(checkins),
                'observed_days': len({item.checkin_date for item in checkins}),
                'journal_entries': len(journals),
                'chat_messages': len(messages),
                'limitations': missing_fields,
                'trigger': trigger,
            },
            model_version=model_version,
            pipeline_version=settings.MINDCARE_PIPELINE_VERSION,
            completed_at=now,
        )
        if existing:
            if existing.status != 'running':
                raise ValueError('Analysis run is not running.')
            for field, value in run_fields.items():
                setattr(existing, field, value)
            existing.save(update_fields=list(run_fields))
            run = existing
        else:
            run = AnalysisRun.objects.create(user=user, analysis_type='deep',
                trigger_source=str(trigger)[:64], period_start=start, period_end=end, **run_fields)
        emotion_summary = report_data['emotion_summary']
        if not isinstance(emotion_summary, dict):
            raise ValueError('Deep analysis adapter emotion_summary must be a dictionary.')
        emotion_summary.update({
            'contributing_factors': report_data.get('contributing_factors', []),
            'dangerous_signals': report_data.get('dangerous_signals', []),
            'limitations': missing_fields,
        })
        if ((checkins and not consent(user, 'ai_checkin')) or
                (journals and not consent(user, 'ai_journal')) or
                (messages and not consent(user, 'ai_chat'))):
            raise PermissionError('Source consent was revoked during analysis.')
        report = AnalysisReport.objects.create(
            analysis_run=run,
            emotion_summary=emotion_summary,
            trend_metrics=context['stress_trend'],
            concern_level=report_data['concern_level'],
            summary=report_data['summary'],
            contributing_factors=report_data.get('contributing_factors', []),
            recommendations=report_data['recommendations'],
        )
        cls._save_sources(run, checkins, journals, messages)
        return {'analysis_run': run, 'report': report, 'context': context}

    @classmethod
    def run_for_queued(cls, run_id, analyzer=None):
        """Entry point for a configured job worker; failures stay visible to polling clients."""
        with transaction.atomic():
            run = AnalysisRun.objects.select_for_update().get(id=run_id, status='queued',
                                                               analysis_type='deep')
            run.status = 'running'
            run.save(update_fields=['status'])
        try:
            return cls.run(run.user, trigger=run.trigger_source, analyzer=analyzer,
                           run_id=run.id)
        except Exception:
            AnalysisRun.objects.filter(id=run.id, status='running').update(
                status='failed', completed_at=timezone.now())
            raise

    @staticmethod
    def _context(checkins, journals, messages, timezone_name):
        user_timezone = ZoneInfo(timezone_name)
        by_day = defaultdict(list)
        by_week = defaultdict(list)
        for item in checkins:
            if item.stress_level is None:
                continue
            local_recorded_at = item.recorded_at.astimezone(user_timezone)
            day = local_recorded_at.date().isoformat()
            by_day[day].append(item.stress_level)
            iso = local_recorded_at.isocalendar()
            by_week[f'{iso.year}-W{iso.week:02d}'].append(item.stress_level)

        daily = [
            {'date': day, 'stress_average': round(sum(values) / len(values), 2), 'check_in_count': len(values)}
            for day, values in sorted(by_day.items())
        ]
        policy = get_analysis_policy()
        weekly = [
            {'week': week, 'stress_average': round(sum(values) / len(values), 2), 'check_in_count': len(values)}
            for week, values in sorted(by_week.items())[-policy.weekly_bucket_limit:]
        ]
        latest = checkins[-1] if checkins else None
        return {
            'stress_trend': {
                'daily': daily,
                'weekly': weekly,
                'missing_periods_estimated': False,
            },
            'current_state': ({
                'stress_level': latest.stress_level,
                'mood_level': latest.mood_level,
                'anxiety_level': latest.anxiety_level,
                'energy_level': latest.energy_level,
                'recorded_at': latest.recorded_at.isoformat(),
            } if latest else None),
            'check_ins': [
                {'id': str(item.id), 'recorded_at': item.recorded_at.isoformat(),
                 'stress_level': item.stress_level, 'mood_level': item.mood_level,
                 'anxiety_level': item.anxiety_level, 'energy_level': item.energy_level,
                 'sleep_hours': item.sleep_hours, 'note': item.note}
                for item in checkins
            ],
            'journals': [
                {'id': str(item.id), 'experienced_at': item.experienced_at.isoformat(), 'content': decrypt(item.content)}
                for item in journals
            ],
            'chat_messages': [
                {'id': str(item.id), 'created_at': item.created_at.isoformat(),
                 'role': item.role, 'content': decrypt(item.content)}
                for item in messages
            ],
        }

    @staticmethod
    def _save_sources(run, checkins, journals, messages):
        AnalysisSource.objects.bulk_create([
            AnalysisSource(analysis_run=run, check_in=item) for item in checkins
        ] + [
            AnalysisSource(analysis_run=run, journal=item) for item in journals
        ] + [
            AnalysisSource(analysis_run=run, chat_message=item) for item in messages
        ])
