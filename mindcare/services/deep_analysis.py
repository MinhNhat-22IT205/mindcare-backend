from collections import defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

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
    def run(cls, user, trigger, analyzer=None, model_version='', now=None):
        now = now or timezone.now()
        policy = get_analysis_policy()
        start = now - timedelta(days=policy.deep_window_days)
        checkins = list(CheckIn.objects.filter(
            user=user, recorded_at__gte=start, recorded_at__lte=now
        ).order_by('recorded_at'))
        journals = list(JournalEntry.objects.filter(
            user=user, experienced_at__gte=start, experienced_at__lte=now
        ).order_by('experienced_at'))
        messages = list(ChatMessage.objects.filter(
            session__user=user, created_at__gte=start, created_at__lte=now
        ).order_by('created_at'))

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
        required_report_fields = {'emotion_summary', 'risk_level', 'summary', 'recommendations'}
        missing_report_fields = required_report_fields - report_data.keys()
        if missing_report_fields:
            raise ValueError(
                'Deep analysis adapter response is missing: '
                + ', '.join(sorted(missing_report_fields))
            )

        run = AnalysisRun.objects.create(
            user=user,
            analysis_type='deep',
            status='completed',
            period_start=start,
            period_end=now,
            sufficiency_status='sufficient' if len(checkins) >= policy.deep_minimum_checkins else 'insufficient',
            missing_fields=missing_fields,
            data_quality={
                'check_ins': len(checkins),
                'journal_entries': len(journals),
                'chat_messages': len(messages),
                'limitations': missing_fields,
                'trigger': trigger,
            },
            model_version=model_version,
            completed_at=now,
        )
        emotion_summary = report_data['emotion_summary']
        if not isinstance(emotion_summary, dict):
            raise ValueError('Deep analysis adapter emotion_summary must be a dictionary.')
        emotion_summary.update({
            'contributing_factors': report_data.get('contributing_factors', []),
            'dangerous_signals': report_data.get('dangerous_signals', []),
            'limitations': missing_fields,
        })
        report = AnalysisReport.objects.create(
            analysis_run=run,
            emotion_summary=emotion_summary,
            trend_metrics=context['stress_trend'],
            concern_level=report_data['risk_level'],
            summary=report_data['summary'],
            contributing_factors=report_data.get('contributing_factors', []),
            recommendations=report_data['recommendations'],
        )
        cls._save_sources(run, checkins, journals, messages)
        return {'analysis_run': run, 'report': report, 'context': context}

    @staticmethod
    def _context(checkins, journals, messages, timezone_name):
        user_timezone = ZoneInfo(timezone_name)
        by_day = defaultdict(list)
        by_week = defaultdict(list)
        for item in checkins:
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
                {'id': str(item.id), 'experienced_at': item.experienced_at.isoformat(), 'content': item.content}
                for item in journals
            ],
            'chat_messages': [
                {'id': str(item.id), 'created_at': item.created_at.isoformat(),
                 'role': item.role, 'content': item.content}
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
