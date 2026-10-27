import json

from django.conf import settings
from django.db import transaction

from mindcare.api.common import (api, body, choice, fail, page, parse_uuid,
                                 respond)
from mindcare.models import AnalysisRun, SafetyAlert, User


@api({'GET'}, {'admin'})
def users(request):
    queryset = User.objects.order_by('-created_at', '-id')
    if role := request.GET.get('role'):
        queryset = queryset.filter(role=choice(role, {'user', 'support', 'admin'}, 'role'))
    if status := request.GET.get('status'):
        queryset = queryset.filter(status=choice(status, {'active', 'inactive'}, 'status'))
    return respond(request, page(request, queryset, lambda u: {
        'id': str(u.id), 'email': u.email, 'display_name': u.display_name,
        'role': u.role, 'status': u.status, 'created_at': u.created_at}))


@api({'PATCH'}, {'admin'})
def user_detail(request, user_id):
    data = body(request, {'role', 'status'})
    if not data:
        fail(422, 'VALIDATION_ERROR')
    with transaction.atomic():
        user = User.objects.select_for_update().filter(id=parse_uuid(user_id)).first()
        if not user:
            fail(404, 'NOT_FOUND')
        new_role = choice(data.get('role', user.role), {'user', 'support', 'admin'}, 'role')
        new_status = choice(data.get('status', user.status), {'active', 'inactive'}, 'status')
        if user.id == request.api_user.id and (new_role != 'admin' or new_status != 'active'):
            fail(409, 'CANNOT_REMOVE_LAST_ADMIN')
        if user.role == 'admin' and user.status == 'active' and (new_role != 'admin' or new_status != 'active'):
            if not User.objects.filter(role='admin', status='active').exclude(id=user.id).exists():
                fail(409, 'CANNOT_REMOVE_LAST_ADMIN')
        user.role, user.status = new_role, new_status
        user.save(update_fields=['role', 'status', 'updated_at'])
    return respond(request, {'id': str(user.id), 'role': user.role,
                             'status': user.status, 'updated_at': user.updated_at})


@api({'GET'}, {'admin'})
def analysis_runs(request):
    queryset = AnalysisRun.objects.order_by('-created_at', '-id')
    if status := request.GET.get('status'):
        queryset = queryset.filter(status=choice(status, {'queued', 'running', 'completed',
                                                          'failed', 'outdated'}, 'status'))
    if kind := request.GET.get('analysis_type'):
        queryset = queryset.filter(analysis_type=choice(kind, {'light', 'deep'}, 'analysis_type'))
    return respond(request, page(request, queryset, lambda r: {
        'id': str(r.id), 'analysis_type': r.analysis_type, 'status': r.status,
        'model_version': r.model_version, 'pipeline_version': r.pipeline_version,
        'created_at': r.created_at, 'completed_at': r.completed_at,
        'sufficiency_status': r.sufficiency_status}))


@api({'GET'}, {'admin'})
def alerts(request):
    queryset = SafetyAlert.objects.order_by('created_at', 'id')
    if request.GET.get('assigned') == 'false':
        queryset = queryset.filter(reviewer_user__isnull=True)
    if status := request.GET.get('status', 'pending_review'):
        queryset = queryset.filter(status=choice(status, {'pending_review', 'approved',
                                                          'rejected', 'cancelled', 'resolved'}, 'status'))
    return respond(request, page(request, queryset, lambda a: {
        'id': str(a.id), 'trigger_type': a.trigger_type, 'severity': a.severity,
        'status': a.status, 'created_at': a.created_at,
        'assigned': a.reviewer_user_id is not None}))


@api({'POST'}, {'admin'})
def alert_assign(request, alert_id):
    data = body(request, {'reviewer_user_id'}, {'reviewer_user_id'})
    with transaction.atomic():
        alert = SafetyAlert.objects.select_for_update().filter(id=parse_uuid(alert_id)).first()
        if not alert:
            fail(404, 'NOT_FOUND')
        if alert.status != 'pending_review' or alert.reviewer_user_id:
            fail(409, 'ALERT_NOT_ASSIGNABLE')
        reviewer = User.objects.filter(id=parse_uuid(data['reviewer_user_id']),
                                       role='support', status='active').first()
        if not reviewer:
            fail(422, 'INVALID_REVIEWER')
        alert.reviewer_user = reviewer
        alert.save(update_fields=['reviewer_user'])
    return respond(request, {'id': str(alert.id), 'status': alert.status,
                             'reviewer_user_id': str(reviewer.id)})


@api({'GET'}, {'admin'})
def rag_status(request):
    manifest_path = getattr(settings, 'MINDCARE_RAG_MANIFEST', '')
    if not manifest_path:
        fail(503, 'RAG_INDEX_UNAVAILABLE')
    try:
        with open(manifest_path, encoding='utf-8') as manifest_file:
            manifest = json.load(manifest_file)
        fields = {'status', 'index_version', 'embedding_model', 'approved_documents', 'built_at'}
        if manifest.get('status') != 'ready' or not fields <= manifest.keys():
            raise ValueError
        expected_model = getattr(settings, 'MINDCARE_RAG_EMBEDDING_MODEL', '')
        if expected_model and manifest['embedding_model'] != expected_model:
            raise ValueError
    except (OSError, ValueError):
        fail(503, 'RAG_INDEX_UNAVAILABLE')
    return respond(request, {key: manifest[key] for key in fields})
