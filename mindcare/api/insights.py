from collections import Counter
from datetime import timedelta
from statistics import mean

from django.utils import timezone
from django.conf import settings
from django.utils.module_loading import import_string

from mindcare.api.common import (api, body, choice, consent, date_range, fail,
                                 local_today, owned, page, parse_datetime, rate_limit, respond)
from mindcare.models import AnalysisReport, AnalysisRun, CheckIn, Notification


def latest_run(user, analysis_type='deep'):
    queryset = AnalysisRun.objects.filter(user=user, analysis_type=analysis_type, status='completed',
                                          report__isnull=False).select_related('report').order_by('-completed_at', '-id')
    return next((run for run in queryset if report_consent_valid(run)), None)


def report_consent_valid(run):
    for field, scope in (('check_in', 'ai_checkin'), ('journal', 'ai_journal'), ('chat_message', 'ai_chat')):
        if run.sources.filter(**{f'{field}__isnull': False}).exists() and not consent(run.user, scope):
            return False
    return True


def report_data(run):
    if not report_consent_valid(run):
        fail(403, 'CONSENT_REQUIRED')
    if run.status in ('queued', 'running'):
        fail(409, 'RESULT_NOT_READY')
    if run.status == 'failed':
        fail(409, 'ANALYSIS_FAILED')
    try:
        report = run.report
    except AnalysisReport.DoesNotExist:
        fail(409, 'REPORT_MISSING')
    return {'run_id': str(run.id), 'status': run.status, 'is_current': run.status == 'completed',
            'period': {'start': run.period_start, 'end': run.period_end},
            'sufficiency': {'status': run.sufficiency_status,
                            'observed_days': run.data_quality.get('observed_days')},
            'report': {'emotion_summary': report.emotion_summary, 'trend_metrics': report.trend_metrics,
                       'concern_level': report.concern_level, 'summary': report.summary,
                       'contributing_factors': report.contributing_factors,
                       'recommendations': report.recommendations}}


@api({'GET'}, {'user'})
def dashboard(request):
    user = request.api_user
    start, end = date_range(request, user)
    rows = list(CheckIn.objects.filter(user=user, checkin_date__range=(start, end)).order_by('checkin_date'))
    by_date = {row.checkin_date: row for row in rows}
    series = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        row = by_date.get(day)
        series.append({'date': day, 'observed': row is not None,
                       'stress_level': row.stress_level if row else None,
                       'mood_level': row.mood_level if row else None,
                       'sleep_hours': row.sleep_hours if row else None})
    run = latest_run(user)
    return respond(request, {
        'period': {'from': start, 'to': end},
        'coverage': {'observed_days': len(rows), 'total_days': (end - start).days + 1},
        'series': series,
        'latest_analysis': ({'run_id': str(run.id), 'analysis_type': run.analysis_type,
                             'status': run.status, 'concern_level': run.report.concern_level} if run else None),
        'pending_notifications': Notification.objects.filter(user=user, is_read=False).count(),
    })


@api({'GET'}, {'user'})
def trends(request):
    metric = choice(request.GET.get('metric', 'stress_level'),
                    {'stress_level', 'mood_level', 'anxiety_level', 'energy_level', 'sleep_hours'}, 'metric')
    try:
        window = int(request.GET.get('window', '7'))
    except ValueError:
        fail(422, 'INVALID_WINDOW')
    if window not in (7, 30):
        fail(422, 'INVALID_WINDOW')
    end = local_today(request.api_user)
    start = end - timedelta(days=window - 1)
    previous_start = start - timedelta(days=window)
    rows = CheckIn.objects.filter(user=request.api_user, checkin_date__gte=previous_start,
                                  checkin_date__lte=end).values('checkin_date', metric)
    previous = [row[metric] for row in rows if row['checkin_date'] < start and row[metric] is not None]
    recent = [row[metric] for row in rows if row['checkin_date'] >= start and row[metric] is not None]
    required = 5 if window == 7 else 20
    enough = len(previous) >= required and len(recent) >= required
    return respond(request, {
        'metric': metric, 'window_days': window,
        'previous': {'observed_days': len(previous), 'avg': round(mean(previous), 2) if previous else None},
        'recent': {'observed_days': len(recent), 'avg': round(mean(recent), 2) if recent else None},
        'change': round(mean(recent) - mean(previous), 2) if enough else None,
        'missing_days': {'previous': window - len(previous), 'recent': window - len(recent)},
        'data_status': 'sufficient_for_descriptive_trend' if enough else 'insufficient_data',
    })


def run_metadata(run):
    return {'id': str(run.id), 'analysis_type': run.analysis_type, 'status': run.status,
            'sufficiency_status': run.sufficiency_status, 'period_start': run.period_start,
            'period_end': run.period_end, 'created_at': run.created_at,
            'completed_at': run.completed_at}


@api({'GET', 'POST'}, {'user'})
def analysis_runs(request):
    user = request.api_user
    if request.method == 'GET':
        queryset = AnalysisRun.objects.filter(user=user).order_by('-created_at', '-id')
        kind = request.GET.get('type')
        status = request.GET.get('status')
        if kind:
            queryset = queryset.filter(analysis_type=choice(kind, {'light', 'deep'}, 'type'))
        if status:
            queryset = queryset.filter(status=choice(status, {'queued', 'running', 'completed', 'failed', 'outdated'}, 'status'))
        return respond(request, page(request, queryset, run_metadata))
    data = body(request, {'analysis_type', 'period_start', 'period_end'},
                {'analysis_type', 'period_start', 'period_end'})
    rate_limit(request, 'analysis', 5, 3600, str(user.id))
    choice(data['analysis_type'], {'deep'}, 'analysis_type')
    start, end = parse_datetime(data['period_start']), parse_datetime(data['period_end'])
    if start >= end or end > timezone.now() + timedelta(minutes=5) or end - start > timedelta(days=90):
        fail(422, 'INVALID_PERIOD')
    if not any(consent(user, scope) for scope in ('ai_checkin', 'ai_journal', 'ai_chat')):
        fail(403, 'CONSENT_REQUIRED')
    enqueue_path = getattr(settings, 'MINDCARE_ANALYSIS_ENQUEUE', '')
    if not enqueue_path:
        fail(503, 'JOB_QUEUE_UNAVAILABLE')
    if AnalysisRun.objects.filter(user=user, analysis_type='deep', status__in=('queued', 'running'),
                                  period_start=start, period_end=end,
                                  pipeline_version=settings.MINDCARE_PIPELINE_VERSION).exists():
        fail(409, 'RUN_IN_PROGRESS')
    run = AnalysisRun.objects.create(user=user, analysis_type='deep', status='queued',
        trigger_source='on_demand', period_start=start, period_end=end,
        sufficiency_status='pending', pipeline_version=settings.MINDCARE_PIPELINE_VERSION)
    try:
        import_string(enqueue_path)(str(run.id))
    except Exception:
        run.status = 'failed'
        run.save(update_fields=['status'])
        fail(503, 'JOB_QUEUE_UNAVAILABLE')
    run.refresh_from_db(fields=['status'])
    return respond(request, {'run_id': str(run.id), 'analysis_type': 'deep', 'status': run.status,
                             'poll_url': f'/api/v1/me/analysis-runs/{run.id}'}, 202)


@api({'GET'}, {'user'})
def analysis_run_detail(request, run_id):
    run = owned(AnalysisRun, request.api_user, run_id)
    result = {**run_metadata(run),
              'data_quality': {key: value for key, value in run.data_quality.items()
                               if key in {'observed_days', 'check_ins', 'journal_entries',
                                          'chat_messages', 'limitations'}},
              'missing_fields': run.missing_fields,
              'report_available': run.status in ('completed', 'outdated') and
              AnalysisReport.objects.filter(analysis_run=run).exists(),
              'cannot_use_as_current': run.status == 'outdated'}
    if result['report_available']:
        result['report_url'] = f'/api/v1/me/analysis-runs/{run.id}/report'
    return respond(request, result)


@api({'GET'}, {'user'})
def analysis_report(request, run_id):
    return respond(request, report_data(owned(AnalysisRun, request.api_user, run_id)))


@api({'GET'}, {'user'})
def latest_report(request):
    kind = choice(request.GET.get('analysis_type', 'deep'), {'light', 'deep'}, 'analysis_type')
    run = latest_run(request.api_user, kind)
    if not run:
        return respond(request, None)
    return respond(request, {'run_id': str(run.id), 'analysis_type': kind,
                             'summary': run.report.summary, 'concern_level': run.report.concern_level,
                             'report_url': f'/api/v1/me/analysis-runs/{run.id}/report'})


@api({'GET'}, {'user'})
def emotion_summary(request):
    source = choice(request.GET.get('source', 'all'), {'journal', 'chat', 'all'}, 'source')
    scopes = {'journal': ('ai_journal',), 'chat': ('ai_chat',), 'all': ('ai_journal', 'ai_chat')}[source]
    if not all(consent(request.api_user, scope) for scope in scopes):
        fail(403, 'CONSENT_REQUIRED')
    start, end = date_range(request, request.api_user, default_days=14)
    reports = AnalysisReport.objects.filter(analysis_run__user=request.api_user,
                                            analysis_run__status='completed',
                                            analysis_run__created_at__date__range=(start, end))
    if source == 'journal':
        reports = reports.filter(analysis_run__sources__journal__isnull=False)
    elif source == 'chat':
        reports = reports.filter(analysis_run__sources__chat_message__isnull=False)
    reports = reports.select_related('analysis_run').distinct()
    counts = Counter()
    observed = 0
    versions = set()
    for report in reports:
        if not report_consent_valid(report.analysis_run):
            continue
        emotion = report.emotion_summary
        if label := emotion.get('top_label'):
            version = report.analysis_run.model_version or 'unknown'
            versions.add(version)
            counts[(version, label)] += 1
            observed += emotion.get('source_count', 1)
    return respond(request, {'period': {'from': start, 'to': end}, 'observed_texts': observed,
                             'emotions': [{'model_version': version, 'label': label, 'count': count}
                                          for (version, label), count in counts.items()],
                             'comparable_models': len(versions) <= 1,
                             'disclaimer': 'Kết quả phân loại văn bản; không phải chẩn đoán.'})


@api({'GET'}, {'user'})
def concern_history(request):
    start, end = date_range(request, request.api_user, default_days=30)
    rows = AnalysisReport.objects.filter(analysis_run__user=request.api_user,
                                         analysis_run__status='completed',
                                         created_at__date__range=(start, end)).select_related('analysis_run').order_by('-created_at')[:50]
    return respond(request, {'items': [{'run_id': str(row.analysis_run_id), 'concern_level': row.concern_level,
                                        'factors': row.contributing_factors, 'is_current': True}
                                       for row in rows if report_consent_valid(row.analysis_run)]})


@api({'GET'}, {'user'})
def report_preview(request):
    start, end = date_range(request, request.api_user)
    allowed = {'stress', 'mood', 'sleep'}
    include = set(request.GET.get('include', 'stress,mood,sleep').split(','))
    if not include <= allowed:
        fail(422, 'INVALID_FILTER')
    rows = list(CheckIn.objects.filter(user=request.api_user, checkin_date__range=(start, end)))
    fields = {'stress': 'stress_level', 'mood': 'mood_level', 'sleep': 'sleep_hours'}
    metrics = {}
    for label in include:
        values = [getattr(row, fields[label]) for row in rows if getattr(row, fields[label]) is not None]
        metrics[f'avg_{label}'] = round(mean(values), 2) if values else None
    return respond(request, {'period': {'from': start, 'to': end}, 'observed_days': len(rows),
                             'missing_days': (end - start).days + 1 - len(rows), 'metrics': metrics,
                             'disclaimer': 'Dữ liệu do người dùng tự báo cáo; không phải chẩn đoán.'})
