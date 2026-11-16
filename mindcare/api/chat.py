from django.db import transaction
from django.utils import timezone

from mindcare.api.common import (api, body, boolean, choice, decrypt,
                                 fail, invalidate_sources, owned, page, parse_uuid,
                                 rate_limit, respond, text_value)
from mindcare.api.records import CHECKIN_FIELDS, checkin_fields
from mindcare.chat_contract import PIPELINE_VERSION
from mindcare.models import (AnalysisReport, AnalysisSource, ChatMessage, ChatSession,
                             CheckIn)
from mindcare.services.chat import public_metadata, send_message


def session_data(item):
    return {'id': str(item.id), 'title': item.title, 'summary': item.summary,
            'followup_enabled': item.state_json.get('followup_enabled', True),
            'created_at': item.created_at, 'updated_at': item.updated_at}


def get_session(user, session_id):
    return owned(ChatSession, user, session_id)


def get_message(user, message_id):
    message = ChatMessage.objects.filter(id=parse_uuid(message_id), session__user=user).first()
    if message is None:
        fail(404, 'NOT_FOUND')
    return message


@api({'GET', 'POST'}, {'user'})
def sessions(request):
    if request.method == 'POST':
        data = body(request, {'title'})
        title = text_value(data.get('title', ''), 'title', 120, blank=True)
        item = ChatSession.objects.create(user=request.api_user, title=title,
                                          state_json={'pipeline_version': PIPELINE_VERSION,
                                                      'followup_enabled': True,
                                                      'last_stage': None, 'last_strategy': None})
        return respond(request, session_data(item), 201)
    queryset = ChatSession.objects.filter(user=request.api_user).order_by('-updated_at', '-id')
    q = request.GET.get('q', '')
    if q:
        queryset = queryset.filter(title__icontains=text_value(q, 'q', 100))
    return respond(request, page(request, queryset, session_data))


@api({'GET', 'PATCH', 'DELETE'}, {'user'})
def session_detail(request, session_id):
    item = get_session(request.api_user, session_id)
    if request.method == 'GET':
        return respond(request, session_data(item))
    if request.method == 'DELETE':
        with transaction.atomic():
            messages = ChatMessage.objects.filter(session=item)
            invalidate_sources('chat_message', messages.values_list('id', flat=True))
            AnalysisReport.objects.filter(analysis_run__sources__chat_message__in=messages).delete()
            AnalysisSource.objects.filter(chat_message__in=messages).delete()
            item.delete()
        return respond(request, status=204)
    data = body(request, {'title', 'followup_enabled'})
    if not data:
        fail(422, 'VALIDATION_ERROR')
    if 'title' in data:
        item.title = text_value(data['title'], 'title', 120, blank=True)
    if 'followup_enabled' in data:
        item.state_json = {**item.state_json,
                           'followup_enabled': boolean(data['followup_enabled'], 'followup_enabled')}
    item.save(update_fields=['title', 'state_json', 'updated_at'])
    return respond(request, session_data(item))


@transaction.non_atomic_requests
@api({'GET', 'POST'}, {'user'})
def messages(request, session_id):
    session = get_session(request.api_user, session_id)
    if request.method == 'GET':
        queryset = ChatMessage.objects.filter(session=session).order_by('-created_at', '-id')
        cursor = request.GET.get('cursor')
        if cursor:
            pivot = ChatMessage.objects.filter(id=parse_uuid(cursor), session=session).first()
            if not pivot:
                fail(422, 'INVALID_CURSOR')
            from django.db.models import Q
            queryset = queryset.filter(Q(created_at__lt=pivot.created_at) |
                                       Q(created_at=pivot.created_at, id__lt=pivot.id))
        try:
            limit = int(request.GET.get('limit', '30'))
        except ValueError:
            fail(422, 'INVALID_PAGINATION')
        if not 1 <= limit <= 100:
            fail(422, 'INVALID_PAGINATION')
        rows = list(queryset[:limit + 1])
        more = len(rows) > limit
        rows = rows[:limit]
        return respond(request, {'items': [{'id': str(m.id), 'role': m.role,
                                            'message_type': m.message_type,
                                            'content': decrypt(m.content),
                                            'metadata': public_metadata(m), 'created_at': m.created_at}
                                           for m in reversed(rows)],
                                 'next_cursor': str(rows[-1].id) if more else None})
    data = body(request, {'message', 'allow_followup_question', 'client_message_id'},
                {'message', 'client_message_id'})
    rate_limit(request, 'chat', 30, 60, str(request.api_user.id))
    if isinstance(data['message'], str) and not data['message'].strip():
        fail(422, 'EMPTY_MESSAGE')
    content = text_value(data['message'], 'message', 4000)
    client_id = text_value(data['client_message_id'], 'client_message_id', 128)
    allow_followup = boolean(data.get('allow_followup_question', True), 'allow_followup_question')
    result = send_message(request.api_user, session.id, content, client_id, allow_followup)
    return respond(request, result, 201)


@api({'PATCH'}, {'user'})
def feedback(request, message_id):
    item = get_message(request.api_user, message_id)
    if item.role != 'assistant':
        fail(404, 'NOT_FOUND')
    data = body(request, {'helpful', 'recommendation_status'})
    if not data:
        fail(422, 'INVALID_FEEDBACK')
    if 'helpful' in data and data['helpful'] is not None:
        boolean(data['helpful'], 'helpful')
    if 'recommendation_status' in data:
        choice(data['recommendation_status'], {'saved', 'tried', 'skipped', 'none'})
    item.metadata = {**item.metadata, 'feedback': {**item.metadata.get('feedback', {}), **data,
                                                'user_feedback_at': timezone.now().isoformat()}}
    item.save(update_fields=['metadata'])
    return respond(request, {'message_id': str(item.id), 'feedback': data})


@api({'POST'}, {'user'})
def confirm_checkin(request, message_id):
    message = get_message(request.api_user, message_id)
    proposal = message.metadata.get('extracted_checkin_proposal')
    if message.role != 'user' or not isinstance(proposal, dict):
        fail(404, 'NOT_FOUND')
    data = body(request, CHECKIN_FIELDS | {'checkin_date', 'confirm'}, {'confirm'})
    confirm = boolean(data['confirm'], 'confirm')
    with transaction.atomic():
        message = ChatMessage.objects.select_for_update().get(pk=message.pk)
        if message.metadata.get('proposal_status') in {'confirmed', 'rejected'}:
            fail(409, 'PROPOSAL_ALREADY_HANDLED')
        if not confirm:
            message.metadata = {**message.metadata, 'proposal_status': 'rejected'}
            message.save(update_fields=['metadata'])
            return respond(request, {'source_message_id': str(message.id), 'proposal_status': 'rejected'})
        from mindcare.api.common import local_today, parse_date
        day = parse_date(data.get('checkin_date', proposal.get('checkin_date')))
        if day > local_today(request.api_user):
            fail(422, 'INVALID_FACT')
        fields = checkin_fields({k: v for k, v in data.items() if k in CHECKIN_FIELDS})
        if not fields:
            fail(422, 'INVALID_FACT')
        item, created = CheckIn.objects.get_or_create(user=request.api_user, checkin_date=day,
                                                       defaults={'recorded_at': timezone.now(), **fields})
        if not created:
            for field, value in fields.items():
                if getattr(item, field) is not None and getattr(item, field) != value:
                    fail(409, 'CONFLICT_WITH_EXISTING_CHECKIN')
                setattr(item, field, value)
            item.save(update_fields=[*fields, 'updated_at'])
        message.metadata = {**message.metadata, 'proposal_status': 'confirmed',
                            'confirmed_at': timezone.now().isoformat()}
        message.save(update_fields=['metadata'])
    return respond(request, {'source_message_id': str(message.id), 'proposal_status': 'confirmed',
                             'checkin_id': str(item.id), 'analysis_queued': False})
