import hashlib
import re
import uuid

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.core.validators import validate_email

from mindcare.api.common import (api, body, boolean, choice, consent,
                                 fail, parse_datetime, parse_uuid, rate_limit, respond,
                                 text_value, token_for, valid_timezone, decrypt)
from mindcare.models import (AlertDelivery, AnalysisReport, AnalysisRun, AnalysisSource, ChatMessage,
                             ChatSession, CheckIn, JournalEntry, Notification,
                             SafetyAlert, SafetyPlan, TrustedContact, User, UserConsent)


def public_user(user):
    return {'id': str(user.id), 'email': user.email, 'display_name': user.display_name,
            'role': user.role, 'status': user.status}


def password(value):
    if not isinstance(value, str):
        fail(422, 'PASSWORD_POLICY_FAILED')
    try:
        validate_password(value)
    except ValidationError:
        fail(422, 'PASSWORD_POLICY_FAILED')
    return value


def normalized_email(value):
    email = text_value(value, 'email', 254).lower()
    try:
        validate_email(email)
    except ValidationError:
        fail(422, 'VALIDATION_ERROR', 'Invalid email address.')
    return email


@api({'POST'})
def register(request):
    rate_limit(request, 'register', 10, 300)
    data = body(request, {'email', 'password', 'display_name', 'timezone'},
                {'email', 'password', 'display_name', 'timezone'})
    email = normalized_email(data['email'])
    secret = password(data['password'])
    name = text_value(data['display_name'], 'display_name', 255)
    tz = valid_timezone(data['timezone'])
    if User.objects.filter(email__iexact=email).exists():
        fail(409, 'EMAIL_ALREADY_EXISTS')
    try:
        with transaction.atomic():
            user = User.objects.create(email=email, password_hash=make_password(secret),
                                       display_name=name, timezone=tz, role='user', status='active')
    except IntegrityError:
        fail(409, 'EMAIL_ALREADY_EXISTS')
    return respond(request, public_user(user), 201)


@api({'POST'})
def login(request):
    data = body(request, {'email', 'password'}, {'email', 'password'})
    email = normalized_email(data['email'])
    rate_limit(request, 'login', 10, 300, email)
    user = User.objects.filter(email__iexact=email).first()
    if not user or not check_password(data['password'], user.password_hash):
        fail(401, 'INVALID_CREDENTIALS')
    if user.status != 'active':
        fail(403, 'ACCOUNT_INACTIVE')
    return respond(request, {'access_token': token_for(user), 'token_type': 'Bearer',
                             'expires_in': 1800, 'user': {
                                 'id': str(user.id), 'role': user.role,
                                 'display_name': user.display_name}})


@api({'POST'}, {'user', 'support', 'admin'})
def logout(request):
    body(request, set())
    return respond(request, status=204)


@api({'POST'})
def reset_request(request):
    data = body(request, {'email'}, {'email'})
    email = normalized_email(data['email'])
    rate_limit(request, 'reset', 5, 3600, email)
    if (not getattr(settings, 'MINDCARE_PASSWORD_RESET_URL', '') or
            (not settings.EMAIL_HOST and not settings.EMAIL_BACKEND.endswith('locmem.EmailBackend'))):
        fail(503, 'MAIL_PROVIDER_UNAVAILABLE')
    user = User.objects.filter(email__iexact=email, status='active').first()
    if user:
        token = signing.dumps({'uid': str(user.id), 'pwd': hashlib.sha256(
            user.password_hash.encode()).hexdigest()}, salt='password-reset')
        link = f"{getattr(settings, 'MINDCARE_PASSWORD_RESET_URL', '')}?uid={user.id}&token={token}"
        try:
            send_mail('MindCare password reset', link, settings.DEFAULT_FROM_EMAIL,
                      [user.email], fail_silently=False)
        except Exception:
            fail(503, 'MAIL_PROVIDER_UNAVAILABLE')
    return respond(request, {'message': 'Nếu tài khoản hợp lệ, hướng dẫn khôi phục đã được gửi.'})


@api({'POST'})
def reset_confirm(request):
    data = body(request, {'uid', 'token', 'new_password'}, {'uid', 'token', 'new_password'})
    secret = password(data['new_password'])
    try:
        payload = signing.loads(data['token'], salt='password-reset', max_age=3600)
        if payload['uid'] != str(uuid.UUID(str(data['uid']))):
            raise ValueError
    except (signing.BadSignature, KeyError, ValueError, TypeError):
        fail(400, 'INVALID_OR_EXPIRED_TOKEN')
    with transaction.atomic():
        user = User.objects.select_for_update().filter(id=payload['uid'], status='active').first()
        if not user or payload['pwd'] != hashlib.sha256(user.password_hash.encode()).hexdigest():
            fail(400, 'INVALID_OR_EXPIRED_TOKEN')
        user.password_hash = make_password(secret)
        user.save(update_fields=['password_hash', 'updated_at'])
    return respond(request, status=204)


@api({'GET', 'PATCH'}, {'user', 'support', 'admin'})
def profile(request):
    user = request.api_user
    if request.method == 'PATCH':
        data = body(request, {'display_name', 'timezone'})
        if not data:
            fail(422, 'VALIDATION_ERROR')
        if 'display_name' in data:
            user.display_name = text_value(data['display_name'], 'display_name', 255)
        if 'timezone' in data:
            user.timezone = valid_timezone(data['timezone'])
        user.save(update_fields=[*data, 'updated_at'])
    result = {**public_user(user), 'timezone': user.timezone,
              'created_at': user.created_at, 'updated_at': user.updated_at}
    return respond(request, result)


@api({'GET', 'PATCH'}, {'user'})
def preferences(request):
    user = request.api_user
    if request.method == 'PATCH':
        data = body(request, {'reminder_enabled', 'reminder_time'})
        if not data:
            fail(422, 'VALIDATION_ERROR')
        if 'reminder_enabled' in data:
            user.reminder_enabled = boolean(data['reminder_enabled'], 'reminder_enabled')
        if 'reminder_time' in data:
            value = data['reminder_time']
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value)):
                fail(422, 'INVALID_REMINDER_TIME')
            user.reminder_time = value or ''
        if user.reminder_enabled and not user.reminder_time:
            fail(422, 'INVALID_REMINDER_TIME')
        user.save(update_fields=[*data, 'updated_at'])
    return respond(request, {'reminder_enabled': user.reminder_enabled,
                             'reminder_time': user.reminder_time or None, 'timezone': user.timezone})


@api({'GET'}, {'user'})
def data_export(request):
    user = request.api_user
    if request.GET.get('format', 'json') != 'json':
        fail(422, 'VALIDATION_ERROR')
    include_chat = request.GET.get('include_chat', 'true') == 'true'
    include_reports = request.GET.get('include_reports', 'true') == 'true'
    payload = {'version': 1, 'exported_at': timezone.now(), 'user': public_user(user),
               'check_ins': list(CheckIn.objects.filter(user=user).values()),
               'journals': [{'id': item.id, 'content': decrypt(item.content),
                             'experienced_at': item.experienced_at}
                            for item in JournalEntry.objects.filter(user=user)],
               'consents': list(UserConsent.objects.filter(user=user).values()),
               'trusted_contacts': [{'id': item.id, 'display_name': item.display_name,
                                     'relationship': item.relationship, 'contact_type': item.contact_type,
                                     'contact_value': decrypt(item.contact_value_encrypted),
                                     'verification_status': item.verification_status}
                                    for item in TrustedContact.objects.filter(user=user)],
               'safety_plans': [{'coping_steps': p.coping_steps, 'personal_notes': decrypt(p.personal_notes)}
                                for p in SafetyPlan.objects.filter(user=user)],
               'alerts': [{'id': item.id, 'status': item.status, 'trigger_type': item.trigger_type,
                           'reason': decrypt(item.reason), 'created_at': item.created_at}
                          for item in SafetyAlert.objects.filter(user=user)],
               'alert_deliveries': list(AlertDelivery.objects.filter(alert__user=user).values(
                   'id', 'alert_id', 'channel', 'delivery_status', 'sent_at', 'delivered_at')),
               'notifications': list(Notification.objects.filter(user=user).values(
                   'id', 'notification_type', 'title', 'safe_message', 'created_at'))}
    if include_chat:
        payload['chat_sessions'] = list(ChatSession.objects.filter(user=user).values('id', 'title', 'created_at'))
        payload['chat_messages'] = [{'id': m.id, 'session_id': m.session_id, 'role': m.role,
                                     'content': decrypt(m.content), 'created_at': m.created_at}
                                    for m in ChatMessage.objects.filter(session__user=user)]
    if include_reports:
        payload['analysis_runs'] = list(AnalysisRun.objects.filter(user=user).values(
            'id', 'analysis_type', 'status', 'period_start', 'period_end'))
        payload['analysis_reports'] = list(AnalysisReport.objects.filter(analysis_run__user=user).values(
            'analysis_run_id', 'emotion_summary', 'trend_metrics', 'concern_level', 'summary',
            'contributing_factors', 'recommendations'))
        payload['analysis_sources'] = list(AnalysisSource.objects.filter(analysis_run__user=user).values(
            'id', 'analysis_run_id', 'check_in_id', 'journal_id', 'chat_message_id'))
    from django.core.serializers.json import DjangoJSONEncoder
    from django.http import HttpResponse
    import json
    response = HttpResponse(json.dumps(payload, cls=DjangoJSONEncoder), content_type='application/json')
    response['Content-Disposition'] = 'attachment; filename="mindcare-export.json"'
    response['Cache-Control'] = 'no-store'
    response['X-Request-Id'] = request.request_id
    return response


@api({'POST'}, {'user'})
def data_deletion(request):
    rate_limit(request, 'delete', 5, 3600, str(request.api_user.id))
    data = body(request, {'password', 'confirmation'}, {'password', 'confirmation'})
    if data['confirmation'] != 'DELETE_MY_ACCOUNT':
        fail(400, 'WRONG_CONFIRMATION')
    user = request.api_user
    if not check_password(data['password'], user.password_hash):
        fail(401, 'INVALID_PASSWORD')
    with transaction.atomic():
        if AlertDelivery.objects.filter(alert__user=user, delivery_status='queued',
                                        alert__status='approved').exists():
            fail(409, 'PENDING_ALERT_DELIVERIES')
        user.delete()
    return respond(request, status=204)


def scope_valid(user, scope):
    if scope in {'ai_checkin', 'ai_journal', 'ai_chat', 'chat_context'}:
        return True
    if isinstance(scope, str) and scope.startswith('share_alert_contact:'):
        contact_id = parse_uuid(scope.split(':', 1)[1])
        return TrustedContact.objects.filter(user=user, id=contact_id).exists()
    return False


@api({'GET', 'POST'}, {'user'})
def consents(request):
    user = request.api_user
    if request.method == 'POST':
        data = body(request, {'scope', 'action', 'policy_version', 'expires_at'},
                    {'scope', 'action', 'policy_version'})
        scope = text_value(data['scope'], 'scope', 128)
        if not scope_valid(user, scope):
            fail(400, 'INVALID_CONSENT_SCOPE')
        action = choice(data['action'], {'grant', 'revoke'}, 'action')
        version = text_value(data['policy_version'], 'policy_version', 64)
        expires = parse_datetime(data['expires_at']) if data.get('expires_at') else None
        if expires and expires <= timezone.now():
            fail(422, 'VALIDATION_ERROR')
        item = UserConsent.objects.create(user=user, scope=scope, action=action,
                                          policy_version=version, expires_at=expires)
        if action == 'revoke':
            source_field = {'ai_checkin': 'check_in', 'ai_journal': 'journal', 'ai_chat': 'chat_message'}.get(scope)
            if source_field:
                AnalysisRun.objects.filter(user=user, status='completed',
                    **{f'sources__{source_field}__isnull': False}).update(status='outdated')
            if scope.startswith('share_alert_contact:'):
                AlertDelivery.objects.filter(trusted_contact_id=scope.split(':', 1)[1],
                    alert__user=user, delivery_status='queued').update(delivery_status='cancelled')
        return respond(request, {'id': str(item.id), 'scope': scope, 'action': action,
                                 'policy_version': version, 'recorded_at': item.recorded_at,
                                 'effective': consent(user, scope)}, 201)
    scope = request.GET.get('scope')
    if scope is not None and not scope_valid(user, scope):
        fail(422, 'INVALID_SCOPE')
    records = UserConsent.objects.filter(user=user).order_by('-recorded_at', '-id')
    if scope:
        records = records.filter(scope=scope)
    effective = {}
    for record in records:
        if record.scope not in effective:
            effective[record.scope] = record.action == 'grant' and (
                record.expires_at is None or record.expires_at > timezone.now())
    result = {'effective_scopes': effective}
    if request.GET.get('include_history', 'true') == 'true':
        result['history'] = [{'id': str(r.id), 'scope': r.scope, 'action': r.action,
                              'policy_version': r.policy_version, 'recorded_at': r.recorded_at,
                              'expires_at': r.expires_at} for r in records]
    return respond(request, result)
