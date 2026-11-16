"""Persisted chat turns, consent-scoped context and single-call ESConv orchestration."""
import hashlib
import json
import logging
import re
from datetime import date, timedelta
from statistics import mean

from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import Exists, OuterRef
from mindcare.api.common import consent, decrypt, encrypt, fail, local_today
from mindcare.chat_contract import (InvalidChatOutput, PIPELINE_VERSION, PROMPT_VERSION, SPACE_ERROR_CODES,
                                    format_messages, input_token_upper_bound, parse_result)
from mindcare.models import (AnalysisRun, ChatMessage, ChatSession, CheckIn,
                             JournalEntry, SafetyAlert)
from mindcare.services.chat_safety import (FALLBACK_RESPONSE, SAFETY_RESPONSE,
                                          asks_question, normalized)
from mindcare.services.configuration import get_configured_callable


logger = logging.getLogger(__name__)
PUBLIC_FIELDS = {'stage', 'strategy', 'status', 'feedback'}
LEGACY_PUBLIC_FIELDS = {'intent', 'action', 'rag_used', 'citations', 'safety_status', 'feedback'}


def public_metadata(message):
    allowed = PUBLIC_FIELDS if message.metadata.get('pipeline_version') == PIPELINE_VERSION else LEGACY_PUBLIC_FIELDS
    return {key: value for key, value in message.metadata.items() if key in allowed}


def reply_data(message, assistant):
    return {
        'user_message': {'id': str(message.id), 'created_at': message.created_at},
        'assistant_message': {'id': str(assistant.id), 'role': 'assistant',
                              'content': decrypt(assistant.content),
                              'metadata': public_metadata(assistant)},
        'analysis_queued': message.metadata.get('analysis_queued') is True,
    }


def request_digest(content, allow_followup):
    return hashlib.sha256(json.dumps([content, allow_followup], ensure_ascii=False).encode()).hexdigest()


def find_existing(session, client_id, content, allow_followup):
    previous = ChatMessage.objects.filter(
        session=session, role='user', metadata__client_message_id=client_id,
    ).first()
    if previous is None:
        return None
    if (decrypt(previous.content) != content or
            previous.metadata.get('request_digest', request_digest(content, allow_followup)) !=
            request_digest(content, allow_followup)):
        fail(409, 'DUPLICATE_MESSAGE_CONFLICT')
    assistant = ChatMessage.objects.filter(session=session, reply_to_message=previous, role='assistant').first()
    if assistant is None:
        fail(409, 'MESSAGE_IN_PROGRESS')
    return reply_data(previous, assistant)


def _ref(kind, item, scopes):
    version = item.updated_at if kind != 'report' else item.completed_at
    return {'kind': kind, 'id': str(item.pk), 'version': version.isoformat() if version else None,
            'scopes': sorted(scopes)}


def sources_valid(user, refs):
    """Recheck permissions and source versions immediately before and after inference."""
    scopes = {scope for ref in refs for scope in ref.get('scopes', ())}
    if any(not consent(user, scope) for scope in scopes):
        return False
    models = {'check_in': CheckIn, 'journal': JournalEntry, 'report': AnalysisRun}
    for ref in refs:
        model = models.get(ref.get('kind'))
        if model is None:
            return False
        filters = {'pk': ref['id'], 'user': user}
        if ref['kind'] == 'report':
            filters['status'] = 'completed'
            filters['completed_at'] = ref['version']
        else:
            filters['updated_at'] = ref['version']
        if not model.objects.filter(**filters).exists():
            return False
    return True


def context_entries(user):
    """Facts and their provenance; never query another session's raw chat text."""
    if not consent(user, 'chat_context'):
        return []
    entries = []
    if consent(user, 'ai_checkin'):
        today = local_today(user)
        rows = list(CheckIn.objects.filter(user=user, checkin_date__range=(today - timedelta(days=13), today))
                    .order_by('checkin_date'))
        refs = [_ref('check_in', row, {'chat_context', 'ai_checkin'}) for row in rows]
        for metric in ('stress_level', 'mood_level', 'anxiety_level', 'energy_level', 'sleep_hours'):
            recent = [getattr(row, metric) for row in rows
                      if row.checkin_date >= today - timedelta(days=6) and getattr(row, metric) is not None]
            previous = [getattr(row, metric) for row in rows
                        if row.checkin_date < today - timedelta(days=6) and getattr(row, metric) is not None]
            enough = len(recent) >= 5 and len(previous) >= 5
            entries.append(({'kind': 'trend', 'metric': metric, 'window_days': 7,
                             'observed_days': len(recent), 'previous_observed_days': len(previous),
                             'recent_average': round(mean(recent), 2) if recent else None,
                             'previous_average': round(mean(previous), 2) if previous else None,
                             'change': round(mean(recent) - mean(previous), 2) if enough else None,
                             'data_status': 'sufficient' if enough else 'insufficient_data'}, refs))
    if consent(user, 'ai_journal'):
        for journal in JournalEntry.objects.filter(user=user).order_by('-experienced_at', '-id')[:3]:
            entries.append(({'kind': 'journal', 'experienced_at': journal.experienced_at.isoformat(),
                             'content': decrypt(journal.content)[:1000]},
                            [_ref('journal', journal, {'chat_context', 'ai_journal'})]))
    runs = AnalysisRun.objects.filter(user=user, status='completed', report__isnull=False).select_related('report')
    for run in runs.order_by('-completed_at', '-id')[:5]:
        sources = list(run.sources.all())
        if not sources:
            continue
        scopes = {'chat_context'}
        for source in sources:
            if source.check_in_id:
                scopes.add('ai_checkin')
            if source.journal_id:
                scopes.add('ai_journal')
            if source.chat_message_id:
                scopes.add('ai_chat')
        if not all(consent(user, scope) for scope in scopes):
            continue
        entries.append(({'kind': 'report', 'summary': run.report.summary[:1000],
                         'trend_metrics': run.report.trend_metrics,
                         'sufficiency_status': run.sufficiency_status,
                         'period_start': run.period_start.isoformat(), 'period_end': run.period_end.isoformat()},
                        [_ref('report', run, scopes)]))
        break
    return entries


def build_context(session, content, followup_allowed):
    budget = settings.MINDCARE_CHAT_MAX_INPUT_TOKENS
    facts, refs, history = [], [], []
    if input_token_upper_bound(format_messages(content, followup_allowed)) > budget:
        fail(422, 'CONTEXT_TOO_LONG')
    # Prioritize recent conversation turns over supplementary data.
    assistants = ChatMessage.objects.filter(
        session=session, role='assistant', reply_to_message__session=session,
        reply_to_message__role='user',
    ).select_related('reply_to_message').order_by('-created_at', '-id')[:30]
    for assistant in assistants:
        source_refs = assistant.metadata.get('source_refs', [])
        if not sources_valid(session.user, source_refs):
            continue
        turn = [{'role': 'user', 'content': decrypt(assistant.reply_to_message.content)},
                {'role': 'assistant', 'content': decrypt(assistant.content)}]
        candidate = turn + history
        if input_token_upper_bound(format_messages(content, followup_allowed, history=candidate)) > budget:
            break
        history = candidate
        refs.extend(source_refs)
    for fact, source_refs in context_entries(session.user):
        candidate = facts + [fact]
        if input_token_upper_bound(format_messages(content, followup_allowed, candidate, history)) <= budget:
            facts = candidate
            refs.extend(source_refs)
    refs = list({(ref['kind'], ref['id']): ref for ref in refs}.values())
    payload = {'messages': format_messages(content, followup_allowed, facts, history),
               'prompt_version': PROMPT_VERSION}
    return payload, refs, facts


def checkin_proposal(content, user):
    """Only explicit, unambiguous numeric facts; no LLM extraction call."""
    value = normalized(content)
    if any(word in value for word in ('gia su', 'neu ', 'vi du', 'maybe', 'suppose')):
        return None
    fields = {}
    patterns = {
        'stress_level': r'\bstress\s*(?:cua (?:toi|minh|em)\s*)?(?:la |: |o muc )?(\d{1,2})\s*/\s*10\b',
        'mood_level': r'\btam trang\s*(?:la |: )?(\d{1,2})\s*/\s*10\b',
        'anxiety_level': r'\blo au\s*(?:la |: )?(\d{1,2})\s*/\s*10\b',
        'energy_level': r'\bnang luong\s*(?:la |: )?(\d{1,2})\s*/\s*10\b',
        'sleep_hours': r'\b(?:toi|minh|em) ngu\s+(\d{1,2}(?:[.,]\d+)?)\s*(?:gio|hours?)\b',
    }
    for field, pattern in patterns.items():
        matches = {float(number.replace(',', '.')) for number in re.findall(pattern, value)}
        if len(matches) > 1:
            return None
        if matches:
            number = matches.pop()
            if not 0 <= number <= (24 if field == 'sleep_hours' else 10):
                return None
            fields[field] = number if field == 'sleep_hours' else int(number)
    if not fields:
        return None
    today = local_today(user)
    dates = set(re.findall(r'\b\d{4}-\d{2}-\d{2}\b', content))
    if len(dates) > 1:
        return None
    if dates:
        try:
            day = date.fromisoformat(dates.pop())
        except ValueError:
            return None
    elif 'hom qua' in value:
        day = today - timedelta(days=1)
    elif 'hom kia' in value:
        day = today - timedelta(days=2)
    elif any(term in value for term in ('hom truoc', 'tuan truoc', 'ngay mai', 'tuan sau', 'last week')):
        return None
    else:
        day = today
    if day > today:
        return None
    return {'checkin_date': day.isoformat(), **fields}


def trend_claim_is_grounded(text, facts):
    value = normalized(text)
    if not re.search(r'(?:stress|lo au|tam trang|xu huong).{0,25}cua ban.{0,25}(?:tang|giam)', value):
        return True
    if not any(f.get('data_status') == 'sufficient' or f.get('sufficiency_status') == 'sufficient' for f in facts):
        return False
    numbers = {7.0, 30.0}  # Calendar windows used by the context builder.

    def collect(item):
        if type(item) in (int, float):
            numbers.add(abs(float(item)))
        elif isinstance(item, dict):
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)
    collect(facts)
    return all(abs(float(number.replace(',', '.'))) in numbers
               for number in re.findall(r'\b\d+(?:[.,]\d+)?\b', value))


def _reserve(user, session_id, content, client_id, allow_followup):
    with transaction.atomic():
        session = ChatSession.objects.select_for_update().filter(pk=session_id, user=user).first()
        if session is None:
            fail(404, 'SESSION_NOT_FOUND')
        if existing := find_existing(session, client_id, content, allow_followup):
            return session, None, None, existing
        replies = ChatMessage.objects.filter(session=session, role='assistant', reply_to_message_id=OuterRef('pk'))
        if ChatMessage.objects.filter(session=session, role='user').annotate(replied=Exists(replies)).filter(replied=False).exists():
            fail(409, 'MESSAGE_IN_PROGRESS')
        followup = session.state_json.get('followup_enabled', True) and allow_followup
        if input_token_upper_bound(format_messages(content, followup)) > settings.MINDCARE_CHAT_MAX_INPUT_TOKENS:
            fail(422, 'CONTEXT_TOO_LONG')
        message = ChatMessage.objects.create(session=session, role='user', message_type='text',
                                             content=encrypt(content), metadata={
                                                 'client_message_id': client_id,
                                                 'request_digest': request_digest(content, allow_followup),
                                                 'allow_followup_question': allow_followup,
                                                 'pipeline_version': PIPELINE_VERSION,
                                                 'analysis_queued': False})
        # A failed safety policy rolls back the reservation; no paid call occurred.
        try:
            signal = get_configured_callable('MINDCARE_SAFETY_DETECTOR')(content)
            if signal is not None and not isinstance(signal, dict):
                signal = {'severity': signal.severity, 'reason': signal.reason}
            if signal is not None and (signal.get('severity') not in {'urgent', 'needs_review'} or
                                       not isinstance(signal.get('reason'), str) or not signal['reason'].strip()):
                raise ValueError('Invalid safety signal.')
        except Exception:
            fail(503, 'SAFETY_DETECTOR_UNAVAILABLE', 'Chưa thể xử lý tin nhắn an toàn lúc này.')
        if signal:
            SafetyAlert.objects.create(user=user, trigger_message=message, trigger_type='chat_signal',
                                       severity=signal['severity'], reason=encrypt(signal['reason'][:1000]),
                                       status='pending_review')
        session.save(update_fields=['updated_at'])
        return session, message, signal, None


def _enqueue_analysis(message):
    if not settings.MINDCARE_CHAT_ANALYSIS_ENQUEUE or not consent(message.session.user, 'ai_chat'):
        return
    try:
        accepted = get_configured_callable('MINDCARE_CHAT_ANALYSIS_ENQUEUE')(str(message.pk))
        queued = accepted is not False
    except Exception:
        logger.warning('Chat light analysis enqueue failed message_id=%s', message.pk)
        queued = False
    message.metadata = {**message.metadata, 'analysis_queued': queued}
    message.save(update_fields=['metadata'])


def send_message(user, session_id, content, client_id, allow_followup):
    try:
        session, message, signal, existing = _reserve(user, session_id, content, client_id, allow_followup)
    except OperationalError as exc:
        # SQLite has no row-level SELECT FOR UPDATE. A concurrent writer must
        # never proceed to inference without committing its reservation.
        if 'locked' in str(exc).lower():
            fail(409, 'MESSAGE_IN_PROGRESS')
        raise
    if existing is not None:
        return existing
    metadata = {'stage': None, 'strategy': None, 'status': 'safety_override' if signal else 'degraded',
                'pipeline_version': PIPELINE_VERSION, 'prompt_version': PROMPT_VERSION,
                'model_ref': settings.MINDCARE_ESCONV_MODEL_REF, 'source_refs': [],
                'safety_status': 'pending_review' if signal else 'degraded',
                'fallback_reason': 'SAFETY_POLICY' if signal else 'MODEL_UNAVAILABLE'}
    reply = SAFETY_RESPONSE if signal else FALLBACK_RESPONSE
    if not signal:
        try:
            session.refresh_from_db()
            followup = session.state_json.get('followup_enabled', True) and allow_followup
            payload, refs, facts = build_context(session, content, followup)
            if not sources_valid(user, refs):
                raise PermissionError('Context consent or source changed.')
            generated = get_configured_callable('MINDCARE_CHAT_RESPONDER')(payload)
            generated = parse_result(generated, settings.MINDCARE_CHAT_MAX_OUTPUT_CHARS)
            session.refresh_from_db()
            followup = session.state_json.get('followup_enabled', True) and allow_followup
            if not sources_valid(user, refs):
                raise PermissionError('Context consent or source changed.')
            if (not followup and (generated['strategy'] == 'questions' or asks_question(generated['response'])) or
                    get_configured_callable('MINDCARE_CHAT_OUTPUT_GUARD')(generated['response']) is not True):
                raise InvalidChatOutput('Output did not pass the local guard.')
            if not trend_claim_is_grounded(generated['response'], facts):
                raise InvalidChatOutput('Personal trend lacks sufficient evidence.')
            reply = generated['response']
            metadata.update(stage=generated['stage'], strategy=generated['strategy'], status='ok',
                            safety_status='checked', source_refs=refs, fallback_reason=None)
        except Exception as exc:
            provider_code = getattr(exc, 'code', None)
            reason = (provider_code if isinstance(provider_code, str) and provider_code in SPACE_ERROR_CODES else
                      'MODEL_TIMEOUT' if isinstance(exc, TimeoutError) else
                      'CONTEXT_CHANGED' if isinstance(exc, PermissionError) else
                      'INVALID_MODEL_OUTPUT' if isinstance(exc, (InvalidChatOutput, ValueError, TypeError)) else
                      'MODEL_UNAVAILABLE')
            metadata['fallback_reason'] = reason
            logger.warning('ESConv fallback reason=%s message_id=%s', reason, message.pk)
    with transaction.atomic():
        session = ChatSession.objects.select_for_update().filter(pk=session_id, user=user).first()
        if session is None:
            fail(404, 'SESSION_NOT_FOUND')
        if metadata['status'] == 'ok':
            # A session PATCH may have disabled questions while inference was
            # running. Recheck under the same lock that protects the final save.
            followup = session.state_json.get('followup_enabled', True) and allow_followup
            changed = not sources_valid(user, metadata['source_refs'])
            questions_blocked = not followup and (metadata['strategy'] == 'questions' or asks_question(reply))
            if changed or questions_blocked:
                reply = FALLBACK_RESPONSE
                metadata.update(stage=None, strategy=None, status='degraded', safety_status='degraded',
                                source_refs=[], fallback_reason='CONTEXT_CHANGED' if changed else 'OUTPUT_BLOCKED')
        assistant = ChatMessage.objects.create(session=session, reply_to_message=message, role='assistant',
                                               message_type='text', content=encrypt(reply), metadata=metadata)
        proposal = checkin_proposal(content, user)
        if proposal:
            message.metadata.update(extracted_checkin_proposal=proposal, proposal_status='pending')
            message.save(update_fields=['metadata'])
        if metadata['status'] == 'ok':
            session.state_json = {**session.state_json, 'last_stage': metadata['stage'],
                                  'last_strategy': metadata['strategy'], 'pipeline_version': PIPELINE_VERSION}
        session.save(update_fields=['state_json', 'updated_at'])
        transaction.on_commit(lambda: _enqueue_analysis(message), robust=True)
    return reply_data(message, assistant)


def process_chat_light_analysis(message_id):
    """Worker entry point for MINDCARE_CHAT_ANALYSIS_ENQUEUE(message_id)."""
    from mindcare.services.light_analysis import LightAnalysisService
    message = ChatMessage.objects.select_related('session__user').get(pk=message_id, role='user')
    if not consent(message.session.user, 'ai_chat'):
        return {'status': 'CONSENT_REQUIRED'}
    return LightAnalysisService.analyze_chat_batch(message)
