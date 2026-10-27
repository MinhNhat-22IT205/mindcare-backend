from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string

from mindcare.api.common import (api, body, boolean, choice, consent, decrypt, encrypt,
                                 fail, invalidate_sources, owned, page, parse_uuid,
                                 rate_limit, respond, text_value)
from mindcare.api.records import CHECKIN_FIELDS, checkin_fields
from mindcare.models import (AnalysisReport, AnalysisSource, ChatMessage, ChatSession,
                             CheckIn, JournalEntry, SafetyAlert)


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


def public_metadata(message):
    allowed = {'intent', 'action', 'rag_used', 'citations', 'safety_status', 'feedback'}
    return {key: value for key, value in message.metadata.items() if key in allowed}


def context_for(user):
    """Only send data covered by current consent to the configured responder."""
    context = {}
    if not consent(user, 'chat_context'):
        return context
    if consent(user, 'ai_checkin'):
        context['check_ins'] = list(CheckIn.objects.filter(user=user).order_by('-checkin_date')
                                    .values('checkin_date', 'stress_level', 'mood_level',
                                            'anxiety_level', 'energy_level', 'sleep_hours')[:7])
    if consent(user, 'ai_journal'):
        context['journals'] = [decrypt(j.content)[:1000] for j in
                               JournalEntry.objects.filter(user=user).order_by('-created_at')[:3]]
    if consent(user, 'ai_chat'):
        context['recent_chat'] = [decrypt(m.content)[:1000] for m in
                                  ChatMessage.objects.filter(session__user=user, role='user')
                                  .order_by('-created_at')[:5]]
    return context


@api({'GET', 'POST'}, {'user'})
def sessions(request):
    if request.method == 'POST':
        data = body(request, {'title'})
        title = text_value(data.get('title', ''), 'title', 120, blank=True)
        item = ChatSession.objects.create(user=request.api_user, title=title,
                                          state_json={'pipeline_version': settings.MINDCARE_PIPELINE_VERSION,
                                                      'followup_enabled': True})
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
    content = text_value(data['message'], 'message', 4000)
    client_id = text_value(data['client_message_id'], 'client_message_id', 128)
    allow_followup = boolean(data.get('allow_followup_question', True), 'allow_followup_question')
    detector_path = getattr(settings, 'MINDCARE_SAFETY_DETECTOR', None)
    if not detector_path:
        fail(503, 'SAFETY_DETECTOR_UNAVAILABLE',
             'Chưa thể xử lý tin nhắn an toàn. Nếu đang gặp nguy hiểm ngay lúc này, hãy liên hệ dịch vụ khẩn cấp tại nơi bạn sống hoặc một người bạn tin tưởng.')
    try:
        signal = import_string(detector_path)(content)
    except Exception:
        fail(503, 'SAFETY_DETECTOR_UNAVAILABLE',
             'Chưa thể xử lý tin nhắn an toàn. Nếu đang gặp nguy hiểm ngay lúc này, hãy liên hệ dịch vụ khẩn cấp tại nơi bạn sống hoặc một người bạn tin tưởng.')
    if signal is not None and not isinstance(signal, dict):
        signal = {'severity': getattr(signal, 'severity', None),
                  'reason': getattr(signal, 'reason', None)}
    if signal is not None and (not isinstance(signal.get('severity'), str) or
                               not isinstance(signal.get('reason'), str)):
        fail(503, 'SAFETY_DETECTOR_UNAVAILABLE')
    reply = None
    proposal = None
    if signal:
        reply = 'Mình đã ghi nhận tín hiệu cần hỗ trợ. Nếu bạn đang gặp nguy hiểm ngay lúc này, hãy liên hệ dịch vụ khẩn cấp tại nơi bạn sống hoặc một người bạn tin tưởng. Yêu cầu hỗ trợ trong ứng dụng đang chờ xem xét.'
        metadata = {'intent': 'safety', 'action': 'OFFER_HUMAN_SUPPORT', 'rag_used': False,
                    'citations': [], 'safety_status': 'pending_review'}
    else:
        responder_path = getattr(settings, 'MINDCARE_CHAT_RESPONDER', None)
        if responder_path:
            try:
                generated = import_string(responder_path)({
                    'message': content, 'followup_enabled': session.state_json.get('followup_enabled', True)
                    and allow_followup, 'context': context_for(request.api_user)})
                reply = text_value(generated['content'], 'assistant response', 4000)
                guard_path = getattr(settings, 'MINDCARE_CHAT_OUTPUT_GUARD', '')
                if not guard_path or import_string(guard_path)(reply) is not True:
                    raise ValueError('Assistant response did not pass output guard.')
                metadata = {'intent': str(generated.get('intent', 'support'))[:64],
                            'action': str(generated.get('action', 'RESPOND'))[:64],
                            'rag_used': bool(generated.get('rag_used', False)), 'citations': [],
                            'safety_status': 'screened'}
                raw_proposal = generated.get('checkin_proposal')
                if isinstance(raw_proposal, dict) and set(raw_proposal) <= CHECKIN_FIELDS | {'checkin_date'}:
                    try:
                        from mindcare.api.common import parse_date
                        parse_date(raw_proposal['checkin_date'])
                        checkin_fields({k: v for k, v in raw_proposal.items() if k in CHECKIN_FIELDS})
                        proposal = raw_proposal
                    except (KeyError, TypeError):
                        proposal = None
            except Exception:
                reply = None
        if reply is None:
            reply = 'Mình chưa thể tạo phản hồi phù hợp lúc này. Bạn có thể thử lại sau; nếu cần hỗ trợ ngay, hãy liên hệ người bạn tin tưởng hoặc dịch vụ khẩn cấp tại nơi bạn sống.'
            metadata = {'intent': 'support', 'action': 'RESPOND', 'rag_used': False,
                        'citations': [], 'safety_status': 'degraded'}
    with transaction.atomic():
        session = ChatSession.objects.select_for_update().get(id=session.id)
        previous = next((m for m in ChatMessage.objects.filter(session=session, role='user')
                         if m.metadata.get('client_message_id') == client_id), None)
        if previous:
            if decrypt(previous.content) != content:
                fail(409, 'DUPLICATE_MESSAGE_CONFLICT')
            assistant = ChatMessage.objects.filter(reply_to_message=previous, role='assistant').first()
            if assistant:
                return respond(request, {'user_message': {'id': str(previous.id), 'created_at': previous.created_at},
                                         'assistant_message': {'id': str(assistant.id), 'role': 'assistant',
                                                               'content': decrypt(assistant.content),
                                                               'metadata': public_metadata(assistant)},
                                         'analysis_queued': False}, 201)
            fail(409, 'DUPLICATE_MESSAGE_CONFLICT')
        message = ChatMessage.objects.create(session=session, role='user', message_type='text',
                                             content=encrypt(content), metadata={
                                                 'client_message_id': client_id,
                                                 **({'extracted_checkin_proposal': proposal,
                                                     'proposal_status': 'pending'} if proposal else {})})
        if signal:
            SafetyAlert.objects.create(user=request.api_user, trigger_message=message,
                                       trigger_type='chat_signal', severity=str(signal['severity'])[:32],
                                       reason=encrypt(str(signal['reason'])[:1000]), status='pending_review')
        assistant = ChatMessage.objects.create(session=session, reply_to_message=message,
                                               role='assistant', message_type='text',
                                               content=encrypt(reply), metadata=metadata)
        session.save(update_fields=['updated_at'])
    return respond(request, {'user_message': {'id': str(message.id), 'created_at': message.created_at},
                             'assistant_message': {'id': str(assistant.id), 'role': 'assistant',
                                                   'content': reply, 'metadata': metadata},
                             'analysis_queued': False}, 201)


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
    item.metadata = {**item.metadata, 'feedback': {**data, 'user_feedback_at': timezone.now().isoformat()}}
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
