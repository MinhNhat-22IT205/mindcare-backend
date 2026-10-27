from dataclasses import dataclass
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string


@dataclass(frozen=True)
class AnalysisPolicy:
    light_window_days: int
    comparison_window_days: int
    change_threshold: float
    change_metrics: tuple[str, ...]
    light_minimum_checkins: int
    deep_minimum_checkins: int
    chat_batch_size: int
    chat_batch_window_hours: int
    deep_window_days: int
    weekly_bucket_limit: int


def get_analysis_policy():
    values = getattr(settings, 'MINDCARE_ANALYSIS_CONFIG', None)
    required = {
        'LIGHT_WINDOW_DAYS', 'COMPARISON_WINDOW_DAYS', 'CHANGE_THRESHOLD',
        'LIGHT_MINIMUM_CHECKINS', 'DEEP_MINIMUM_CHECKINS',
        'CHAT_BATCH_SIZE', 'CHAT_BATCH_WINDOW_HOURS',
        'DEEP_WINDOW_DAYS', 'WEEKLY_BUCKET_LIMIT', 'CHANGE_METRICS',
    }
    if not isinstance(values, dict) or (missing := required - values.keys()):
        missing_names = ', '.join(sorted(missing)) if isinstance(values, dict) else ', '.join(sorted(required))
        raise ImproperlyConfigured(f'MINDCARE_ANALYSIS_CONFIG is missing: {missing_names}')
    configured_metrics = values.get('CHANGE_METRICS', ())
    if isinstance(configured_metrics, str):
        configured_metrics = configured_metrics.split(',')
    try:
        policy = AnalysisPolicy(
            light_window_days=int(values['LIGHT_WINDOW_DAYS']),
            comparison_window_days=int(values['COMPARISON_WINDOW_DAYS']),
            change_threshold=float(values['CHANGE_THRESHOLD']),
            change_metrics=tuple(str(item).strip() for item in configured_metrics if str(item).strip()),
            light_minimum_checkins=int(values['LIGHT_MINIMUM_CHECKINS']),
            deep_minimum_checkins=int(values['DEEP_MINIMUM_CHECKINS']),
            chat_batch_size=int(values['CHAT_BATCH_SIZE']),
            chat_batch_window_hours=int(values['CHAT_BATCH_WINDOW_HOURS']),
            deep_window_days=int(values['DEEP_WINDOW_DAYS']),
            weekly_bucket_limit=int(values['WEEKLY_BUCKET_LIMIT']),
        )
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured('MINDCARE_ANALYSIS_CONFIG values must be numeric.') from exc
    if not policy.change_metrics:
        raise ImproperlyConfigured('MINDCARE_ANALYSIS_CONFIG.CHANGE_METRICS must contain at least one model field.')
    if min(
        policy.light_window_days, policy.comparison_window_days,
        policy.light_minimum_checkins, policy.deep_minimum_checkins,
        policy.chat_batch_size,
        policy.chat_batch_window_hours, policy.deep_window_days,
        policy.weekly_bucket_limit,
    ) < 1 or policy.change_threshold < 0:
        raise ImproperlyConfigured('MINDCARE_ANALYSIS_CONFIG values are outside their valid range.')
    return policy


def get_configured_callable(setting_name):
    """Resolve an injected callable or dotted import path from Django settings."""
    configured = getattr(settings, setting_name, None)
    if configured is None:
        raise ImproperlyConfigured(f'{setting_name} must be configured for this workflow.')
    if isinstance(configured, str):
        configured = import_string(configured)
    if not callable(configured):
        raise ImproperlyConfigured(f'{setting_name} must point to a callable.')
    return configured
