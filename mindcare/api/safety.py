import re

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string

from mindcare.api.common import (api, body, boolean, choice, consent, decrypt,
                                 encrypt, fail, owned, page, parse_uuid, rate_limit, respond,
                                 signed_invitation, text_value, verified_invitation)
from mindcare.models import (AlertDelivery, Notification, SafetyAlert, SafetyPlan,
                             TrustedContact, UserConsent)


def plan_data(plan):
    return {'user_id': str(plan.user_id), 'enabled': plan.enabled,
            'coping_steps': plan.coping_steps,
            'personal_notes': decrypt(plan.personal_notes) if plan.personal_notes else '',
            'updated_at': plan.updated_at}


@api({'GET', 'PUT'}, {'user'})
def safety_plan(request):
    user = request.api_user
    if request.method == 'GET':
        plan = SafetyPlan.objects.filter(user=user).first()
        return respond(request, plan_data(plan) if plan else None)
    data = body(request, {'enabled', 'coping_steps', 'personal_notes'},
                {'enabled', 'coping_steps', 'personal_notes'})
    enabled = boolean(data['enabled'], 'enabled')
    steps = data['coping_steps']
    if not isinstance(steps, list) or len(steps) > 20 or any(
            not isinstance(s, dict) or set(s) != {'order', 'title'} or
            type(s['order']) is not int or s['order'] < 1 or
            not isinstance(s['title'], str) or not 1 <= len(s['title']) <= 200
            for s in steps):
        fail(422, 'INVALID_SAFETY_PLAN')
    notes = text_value(data['personal_notes'], 'personal_notes', 5000, blank=True)
    plan, _ = SafetyPlan.objects.update_or_create(user=user, defaults={
        'enabled': enabled, 'coping_steps': steps, 'personal_notes': encrypt(notes) if notes else ''})
    return respond(request, plan_data(plan))


def contact_value(kind, value):
    value = text_value(value, 'contact_value', 254)
    if kind == 'email':
        from django.core.validators import validate_email
        from django.core.exceptions import ValidationError
        try:
            validate_email(value)
        except ValidationError:
            fail(422, 'INVALID_CONTACT')
        return value.lower()
    if kind == 'phone' and re.fullmatch(r'\+?[0-9]{8,15}', value):
        return value
    fail(422, 'INVALID_CONTACT')


def contact_data(item):
    return {'id': str(item.id), 'display_name': item.display_name,
            'relationship': item.relationship, 'contact_type': item.contact_type,
            'contact_value': decrypt(item.contact_value_encrypted),
            'verification_status': item.verification_status,
            'verified_at': item.verified_at,
            'alert_sharing_enabled': item.verification_status == 'verified' and
            consent(item.user, f'share_alert_contact:{item.id}')}


def revoke_contact(item):
    scope = f'share_alert_contact:{item.id}'
    if consent(item.user, scope):
        UserConsent.objects.create(user=item.user, scope=scope, action='revoke', policy_version='v1.0')
    AlertDelivery.objects.filter(trusted_contact=item, delivery_status='queued').update(delivery_status='cancelled')


@api({'GET', 'POST'}, {'user'})
def contacts(request):
    user = request.api_user
    if request.method == 'GET':
        return respond(request, {'items': [contact_data(c) for c in TrustedContact.objects.filter(
            user=user).order_by('-created_at')]})
    data = body(request, {'display_name', 'relationship', 'contact_type', 'contact_value'},
                {'display_name', 'relationship', 'contact_type', 'contact_value'})
    if TrustedContact.objects.filter(user=user).count() >= 10:
        fail(409, 'CONTACT_LIMIT_REACHED')
    kind = choice(data['contact_type'], {'email', 'phone'}, 'contact_type')
    address = contact_value(kind, data['contact_value'])
    for existing in TrustedContact.objects.filter(user=user, contact_type=kind):
        if decrypt(existing.contact_value_encrypted) == address:
            fail(409, 'CONTACT_ALREADY_EXISTS')
    item = TrustedContact.objects.create(user=user,
        display_name=text_value(data['display_name'], 'display_name', 255),
        relationship=text_value(data['relationship'], 'relationship', 64),
        contact_type=kind, contact_value_encrypted=encrypt(address),
        verification_status='pending')
    return respond(request, {'id': str(item.id), 'display_name': item.display_name,
                             'contact_type': kind, 'verification_status': 'pending'}, 201)


@api({'PATCH', 'DELETE'}, {'user'})
def contact_detail(request, contact_id):
    item = owned(TrustedContact, request.api_user, contact_id)
    if request.method == 'DELETE':
        with transaction.atomic():
            revoke_contact(item)
            item.delete()
        return respond(request, status=204)
    data = body(request, {'display_name', 'relationship', 'contact_type', 'contact_value'})
    if not data:
        fail(422, 'VALIDATION_ERROR')
    old_kind = item.contact_type
    old_value = decrypt(item.contact_value_encrypted)
    new_kind = choice(data.get('contact_type', old_kind), {'email', 'phone'}, 'contact_type')
    new_value = contact_value(new_kind, data.get('contact_value', old_value))
    if (old_kind, old_value) != (new_kind, new_value):
        for other in TrustedContact.objects.filter(user=request.api_user, contact_type=new_kind).exclude(id=item.id):
            if decrypt(other.contact_value_encrypted) == new_value:
                fail(409, 'CONTACT_ALREADY_EXISTS')
    with transaction.atomic():
        if (old_kind, old_value) != (new_kind, new_value):
            revoke_contact(item)
            item.contact_type = new_kind
            item.contact_value_encrypted = encrypt(new_value)
            item.verification_status = 'pending'
            item.verified_at = None
        if 'display_name' in data:
            item.display_name = text_value(data['display_name'], 'display_name', 255)
        if 'relationship' in data:
            item.relationship = text_value(data['relationship'], 'relationship', 64)
        item.save()
    return respond(request, {'id': str(item.id), 'display_name': item.display_name,
                             'verification_status': item.verification_status,
                             'verified_at': item.verified_at})


@api({'POST'}, {'user'})
def contact_verification(request, contact_id):
    body(request, set())
    rate_limit(request, 'contact-invite', 5, 3600, str(request.api_user.id))
    item = owned(TrustedContact, request.api_user, contact_id)
    if item.verification_status != 'pending':
        fail(409, 'ALREADY_VERIFIED')
    if item.contact_type != 'email' or not getattr(settings, 'MINDCARE_CONTACT_INVITATION_URL', ''):
        fail(503, 'PROVIDER_UNAVAILABLE')
    token = signed_invitation(item)
    link = f'{settings.MINDCARE_CONTACT_INVITATION_URL}?token={token}'
    try:
        send_mail('MindCare contact invitation', link, settings.DEFAULT_FROM_EMAIL,
                  [decrypt(item.contact_value_encrypted)], fail_silently=False)
    except Exception:
        fail(503, 'PROVIDER_UNAVAILABLE')
    return respond(request, {'contact_id': str(item.id), 'verification_request_status': 'sent',
                             'expires_in_seconds': 86400}, 202)


def invitation(request, decision):
    data = body(request, {'token'}, {'token'})
    item = verified_invitation(data['token'])
    with transaction.atomic():
        item = TrustedContact.objects.select_for_update().get(pk=item.pk)
        if item.verification_status != 'pending':
            fail(409, 'INVITATION_ALREADY_HANDLED')
        if decision == 'declined':
            revoke_contact(item)
        item.verification_status = decision
        item.verified_at = timezone.now() if decision == 'verified' else None
        item.save(update_fields=['verification_status', 'verified_at'])
    return respond(request, status=204)


@api({'POST'})
def invitation_accept(request):
    return invitation(request, 'verified')


@api({'POST'})
def invitation_decline(request):
    return invitation(request, 'declined')


@api({'POST'}, {'user'})
def support_request(request):
    rate_limit(request, 'support-request', 10, 3600, str(request.api_user.id))
    data = body(request, {'message', 'preferred_support'}, {'message'})
    text = text_value(data['message'], 'message', 1000)
    if 'preferred_support' in data:
        choice(data['preferred_support'], {'human'}, 'preferred_support')
    alert = SafetyAlert.objects.create(user=request.api_user, trigger_type='user_request',
        severity='needs_review', reason=encrypt(text), status='pending_review')
    Notification.objects.create(user=request.api_user, safety_alert=alert,
        notification_type='support_request', title='Yêu cầu đã được ghi nhận',
        safe_message='Yêu cầu đang chờ xem xét; chưa có xác nhận đã tiếp nhận bởi người hỗ trợ.')
    return respond(request, {'alert_id': str(alert.id), 'status': alert.status,
        'message': 'Yêu cầu đã được ghi nhận; chưa có xác nhận đã tiếp nhận bởi người hỗ trợ.'}, 202)


def delivery_data(item):
    return {'id': str(item.id), 'channel': item.channel,
            'delivery_status': item.delivery_status, 'sent_at': item.sent_at,
            'delivered_at': item.delivered_at, 'error_code': item.error_code or None}


def alert_data(item, include_reason=False):
    result = {'id': str(item.id), 'trigger_type': item.trigger_type,
              'severity': item.severity, 'status': item.status,
              'created_at': item.created_at, 'reviewed_at': item.reviewed_at,
              'resolved_at': item.resolved_at}
    if include_reason:
        result['reason'] = decrypt(item.reason)
    return result


@api({'GET'}, {'user'})
def alerts(request):
    queryset = SafetyAlert.objects.filter(user=request.api_user).order_by('-created_at', '-id')
    status = request.GET.get('status')
    if status:
        queryset = queryset.filter(status=choice(status, {'pending_review', 'approved', 'rejected',
                                                          'cancelled', 'resolved'}, 'status'))
    return respond(request, page(request, queryset, lambda a: {
        **alert_data(a), 'sent_count': AlertDelivery.objects.filter(alert=a,
                                              delivery_status__in=('sent', 'delivered')).count()}))


@api({'GET'}, {'user'})
def alert_detail(request, alert_id):
    item = owned(SafetyAlert, request.api_user, alert_id)
    return respond(request, {**alert_data(item, True),
                             'deliveries': [delivery_data(d) for d in item.deliveries.order_by('-id')]})


@api({'POST'}, {'user'})
def alert_cancel(request, alert_id):
    body(request, {'reason'})
    with transaction.atomic():
        item = SafetyAlert.objects.select_for_update().filter(id=parse_uuid(alert_id), user=request.api_user).first()
        if not item:
            fail(404, 'NOT_FOUND')
        if item.status not in {'pending_review', 'approved'}:
            fail(409, 'INVALID_ALERT_TRANSITION')
        if item.deliveries.exclude(delivery_status='queued').exists():
            fail(409, 'ALERT_ALREADY_DISPATCHED')
        item.status = 'cancelled'
        item.resolved_at = timezone.now()
        item.save(update_fields=['status', 'resolved_at'])
        item.deliveries.filter(delivery_status='queued').update(delivery_status='cancelled')
    return respond(request, {'id': str(item.id), 'status': item.status, 'resolved_at': item.resolved_at})


def assigned_alert(user, alert_id):
    item = SafetyAlert.objects.filter(id=parse_uuid(alert_id), reviewer_user=user).first()
    if not item:
        fail(404, 'NOT_FOUND')
    return item


@api({'GET'}, {'support'})
def support_alerts(request):
    queryset = SafetyAlert.objects.filter(reviewer_user=request.api_user).order_by('-created_at', '-id')
    if status := request.GET.get('status'):
        queryset = queryset.filter(status=choice(status, {'pending_review', 'approved', 'rejected',
                                                          'cancelled', 'resolved'}, 'status'))
    return respond(request, page(request, queryset, alert_data))


@api({'GET'}, {'support'})
def support_alert_detail(request, alert_id):
    item = assigned_alert(request.api_user, alert_id)
    return respond(request, {**alert_data(item, True),
        'allowed_actions': ['approve', 'reject'] if item.status == 'pending_review' else []})


@api({'POST'}, {'support'})
def review_alert(request, alert_id):
    data = body(request, {'decision', 'reason'}, {'decision', 'reason'})
    decision = choice(data['decision'], {'approve', 'reject'}, 'decision')
    text_value(data['reason'], 'reason', 1000)
    with transaction.atomic():
        item = SafetyAlert.objects.select_for_update().filter(id=parse_uuid(alert_id),
                                       reviewer_user=request.api_user).first()
        if not item:
            fail(404, 'NOT_FOUND')
        if item.status != 'pending_review':
            fail(409, 'ALERT_ALREADY_REVIEWED')
        item.status = 'approved' if decision == 'approve' else 'rejected'
        item.reviewed_at = timezone.now()
        item.save(update_fields=['status', 'reviewed_at'])
        eligible = []
        if decision == 'approve':
            eligible = [c for c in TrustedContact.objects.filter(user=item.user,
                        verification_status='verified') if consent(item.user, f'share_alert_contact:{c.id}')]
            if eligible and not getattr(settings, 'MINDCARE_ALERT_DELIVERY_ENQUEUE', ''):
                fail(503, 'JOB_QUEUE_UNAVAILABLE')
            AlertDelivery.objects.bulk_create([AlertDelivery(alert=item, trusted_contact=c,
                channel=c.contact_type, delivery_status='queued') for c in eligible])
    if eligible:
        try:
            import_string(settings.MINDCARE_ALERT_DELIVERY_ENQUEUE)(str(item.id))
        except Exception:
            AlertDelivery.objects.filter(alert=item, delivery_status='queued').update(
                delivery_status='failed', error_code='enqueue_failed')
            fail(503, 'JOB_QUEUE_UNAVAILABLE')
    return respond(request, {'id': str(item.id), 'status': item.status,
                             'reviewed_at': item.reviewed_at, 'eligible_recipients': len(eligible),
                             'deliveries_queued': len(eligible)})


@api({'GET'}, {'support'})
def support_deliveries(request, alert_id):
    item = assigned_alert(request.api_user, alert_id)
    return respond(request, {'alert_id': str(item.id),
        'items': [delivery_data(d) for d in item.deliveries.order_by('-sent_at', '-id')]})


@api({'GET'}, {'user'})
def notifications(request):
    queryset = Notification.objects.filter(user=request.api_user).order_by('-created_at', '-id')
    if request.GET.get('unread_only', 'false') == 'true':
        queryset = queryset.filter(is_read=False)
    result = page(request, queryset, lambda n: {'id': str(n.id),
        'notification_type': n.notification_type, 'title': n.title,
        'safe_message': n.safe_message, 'is_read': n.is_read,
        'created_at': n.created_at})
    result['unread_count'] = Notification.objects.filter(user=request.api_user, is_read=False).count()
    return respond(request, result)


@api({'PATCH'}, {'user'})
def notification_detail(request, notification_id):
    data = body(request, {'is_read'}, {'is_read'})
    item = owned(Notification, request.api_user, notification_id)
    item.is_read = boolean(data['is_read'], 'is_read')
    item.read_at = timezone.now() if item.is_read else None
    item.save(update_fields=['is_read', 'read_at'])
    return respond(request, {'id': str(item.id), 'is_read': item.is_read, 'read_at': item.read_at})
