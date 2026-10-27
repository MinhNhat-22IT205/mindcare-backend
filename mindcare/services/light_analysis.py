from datetime import timedelta

from django.core.exceptions import FieldDoesNotExist
from django.db import models, transaction
from django.utils import timezone

from mindcare.models import (
    AnalysisRun,
    AnalysisSource,
    CheckIn,
    ChatMessage,
    JournalEntry,
)
from mindcare.services.configuration import get_analysis_policy
from mindcare.services.safety import screen_chat_message


class LightAnalysisService:
    """Rule-based sufficiency and change detection; never invents missing values."""

    @classmethod
    def screen_message(cls, message: ChatMessage, detector=None):
        # Safety screening is per-message and runs even when no light batch is due.
        if detector is None:
            return screen_chat_message(message)
        return screen_chat_message(message, detector=detector)

    @classmethod
    def analyze_chat_batch(cls, message: ChatMessage, detector=None, now=None):
        cls.screen_message(message, detector=detector)
        policy = get_analysis_policy()
        recent_count = ChatMessage.objects.filter(
            session=message.session,
            created_at__gte=(now or timezone.now()) - timedelta(hours=policy.chat_batch_window_hours),
        ).count()
        if recent_count < policy.chat_batch_size:
            return {'status': 'WAITING_FOR_BATCH', 'messages_seen': recent_count}
        return cls.analyze(message.session.user, trigger_message=message, now=now)

    @classmethod
    @transaction.atomic
    def analyze(cls, user, trigger_message=None, now=None):
        now = now or timezone.now()
        policy = get_analysis_policy()
        start = now - timedelta(days=policy.light_window_days)
        prior_start = start - timedelta(days=policy.comparison_window_days)
        checkins = list(CheckIn.objects.filter(
            user=user, recorded_at__gte=prior_start, recorded_at__lt=now
        ).order_by('recorded_at'))
        recent = [item for item in checkins if item.recorded_at >= start]
        journals = list(JournalEntry.objects.filter(
            user=user, experienced_at__gte=start, experienced_at__lte=now
        ).order_by('experienced_at'))
        messages = list(ChatMessage.objects.filter(
            session__user=user, created_at__gte=start, created_at__lte=now
        ).order_by('created_at'))

        missing_fields = []
        if not recent:
            missing_fields.append(f'check_ins:last_{policy.light_window_days}_days')
        if not journals:
            missing_fields.append(f'journal_entries:last_{policy.light_window_days}_days')
        if not messages:
            missing_fields.append(f'chat_messages:last_{policy.light_window_days}_days')
        if len(recent) < policy.light_minimum_checkins:
            missing_fields.append(f'check_ins:{policy.light_minimum_checkins}_or_more_for_change_detection')

        changes = cls._detect_changes(recent, checkins, start, policy)
        decision = 'RUN_DEEP' if changes else 'CONTINUE'
        sufficient = len(recent) >= policy.light_minimum_checkins
        run = AnalysisRun.objects.create(
            user=user,
            analysis_type='light',
            status='completed',
            period_start=start,
            period_end=now,
            sufficiency_status='sufficient' if sufficient else 'insufficient',
            missing_fields=missing_fields,
            data_quality={
                f'check_ins_last_{policy.light_window_days}_days': len(recent),
                f'check_ins_previous_{policy.comparison_window_days}_days': len(checkins) - len(recent),
                f'journal_entries_last_{policy.light_window_days}_days': len(journals),
                f'chat_messages_last_{policy.light_window_days}_days': len(messages),
                'change_threshold': policy.change_threshold,
                'comparison_window_days': policy.comparison_window_days,
                'changes': changes,
            },
            completed_at=now,
        )
        cls._save_sources(run, recent, journals, messages)
        return {
            'analysis_run': run,
            'decision': decision,
            'missing_fields': missing_fields,
            'changes': changes,
        }

    @staticmethod
    def _detect_changes(recent, all_checkins, cutoff, policy):
        older = [item for item in all_checkins if item.recorded_at < cutoff]
        changes = {}
        for metric in policy.change_metrics:
            try:
                field = CheckIn._meta.get_field(metric)
            except FieldDoesNotExist as exc:
                raise ValueError(f'Configured change metric {metric!r} is not a CheckIn field.')
            if not isinstance(field, (models.IntegerField, models.FloatField, models.DecimalField)):
                raise ValueError(f'Configured change metric {metric!r} must be numeric.')
            current_values = [getattr(item, metric) for item in recent]
            older_values = [getattr(item, metric) for item in older]
            if not current_values or not older_values:
                continue
            current_avg = sum(current_values) / len(current_values)
            previous_avg = sum(older_values) / len(older_values)
            delta = round(current_avg - previous_avg, 2)
            if abs(delta) >= policy.change_threshold:
                changes[metric] = {
                    'previous_average': round(previous_avg, 2),
                    'current_average': round(current_avg, 2),
                    'change': delta,
                }
        return changes

    @staticmethod
    def _save_sources(run, checkins, journals, messages):
        AnalysisSource.objects.bulk_create([
            AnalysisSource(analysis_run=run, check_in=item) for item in checkins
        ] + [
            AnalysisSource(analysis_run=run, journal=item) for item in journals
        ] + [
            AnalysisSource(analysis_run=run, chat_message=item) for item in messages
        ])
