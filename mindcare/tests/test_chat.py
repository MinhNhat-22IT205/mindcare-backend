import importlib.util
import json
import tempfile
from contextlib import nullcontext
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from mindcare import chat_contract
from mindcare.api.common import decrypt, encrypt, token_for
from mindcare.api.swagger import build_openapi_schema
from mindcare.chat_contract import PIPELINE_VERSION, PROMPT_VERSION, SpaceInferenceError, parse_result
from mindcare.integrations.esconv import ProviderUnavailable, respond as space_respond
from mindcare.models import (AlertDelivery, AnalysisReport, AnalysisRun, AnalysisSource,
                             ChatMessage, ChatSession, CheckIn, JournalEntry, SafetyAlert, User,
                             UserConsent)
from mindcare.services.chat import process_chat_light_analysis
from mindcare.services.chat_safety import detect_safety, guard_output


TEST_KEY = 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA='
GOOD_REPLY = {'stage': 'comforting', 'strategy': 'reflection_of_feelings',
              'response': 'Có vẻ áp lực đồ án đang khiến bạn mệt mỏi. Mình sẵn sàng lắng nghe bạn.'}


@override_settings(MINDCARE_DATA_ENCRYPTION_KEY=TEST_KEY,
                   MINDCARE_SAFETY_DETECTOR='mindcare.services.chat_safety.detect_safety',
                   MINDCARE_CHAT_OUTPUT_GUARD='mindcare.services.chat_safety.guard_output',
                   MINDCARE_CHAT_MAX_INPUT_TOKENS=8192, MINDCARE_CHAT_ANALYSIS_ENQUEUE='')
class ChatTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create(email='chat@example.com', display_name='Chat',
                                        role='user', status='active', timezone='Asia/Ho_Chi_Minh')
        self.other = User.objects.create(email='other@example.com', display_name='Other',
                                         role='user', status='active', timezone='UTC')
        self.session = ChatSession.objects.create(user=self.user, state_json={'followup_enabled': True,
                                                                              'pipeline_version': PIPELINE_VERSION})
        self.path = f'/api/v1/me/chat/sessions/{self.session.pk}/messages'
        self.headers = {'HTTP_AUTHORIZATION': f'Bearer {token_for(self.user)}'}
        self.responder = Mock(return_value=GOOD_REPLY.copy())
        self.adapter = override_settings(MINDCARE_CHAT_RESPONDER=self.responder)
        self.adapter.enable()
        self.addCleanup(self.adapter.disable)

    def post(self, message='Hôm nay mình thấy áp lực.', client_id='turn-1', **extra):
        return self.client.post(self.path, data=json.dumps({'message': message,
                                'client_message_id': client_id, **extra}),
                                content_type='application/json', **self.headers)

    def grant(self, *scopes):
        for scope in scopes:
            UserConsent.objects.create(user=self.user, scope=scope, action='grant', policy_version='v1')

    def payload(self):
        return self.responder.call_args.args[0]

    def test_one_generation_and_private_metadata(self):
        response = self.post()
        self.assertEqual(response.status_code, 201)
        self.responder.assert_called_once()
        self.assertEqual(self.payload()['prompt_version'], PROMPT_VERSION)
        messages = self.payload()['messages']
        self.assertEqual([m['role'] for m in messages], ['system', 'user'])
        self.assertEqual(json.loads(messages[-1]['content'])['message'], 'Hôm nay mình thấy áp lực.')
        self.assertEqual(response.json()['data']['assistant_message']['metadata'],
                         {'stage': 'comforting', 'strategy': 'reflection_of_feelings', 'status': 'ok'})
        self.assertEqual(ChatMessage.objects.count(), 2)
        self.assertNotIn('áp lực', ChatMessage.objects.get(role='user').content)
        self.session.refresh_from_db()
        self.assertEqual(self.session.state_json['last_stage'], 'comforting')
        stored = ChatMessage.objects.get(role='assistant')
        self.assertEqual(stored.metadata['prompt_version'], PROMPT_VERSION)
        self.assertNotIn('prompt', stored.metadata)
        history = self.client.get(self.path, **self.headers).json()['data']['items']
        self.assertEqual(history[-1]['metadata'], response.json()['data']['assistant_message']['metadata'])

    def test_session_creation_does_not_call_model(self):
        response = self.client.post('/api/v1/me/chat/sessions', data='{}', content_type='application/json',
                                    **self.headers)
        self.assertEqual(response.status_code, 201)
        state = ChatSession.objects.get(pk=response.json()['data']['id']).state_json
        self.assertIsNone(state['last_stage'])
        self.assertIsNone(state['last_strategy'])
        self.responder.assert_not_called()

    def test_retry_reuses_messages_without_second_call(self):
        first = self.post().json()['data']
        second = self.post().json()['data']
        self.assertEqual(first, second)
        self.responder.assert_called_once()
        self.assertEqual(ChatMessage.objects.count(), 2)
        self.assertEqual(self.post(message='Nội dung khác').status_code, 409)
        self.assertEqual(self.post(allow_followup_question=False).status_code, 409)
        self.responder.assert_called_once()

    def test_reservation_prevents_reentrant_or_parallel_inference(self):
        def reply(payload):
            self.assertTrue(ChatMessage.objects.filter(session=self.session, role='user').exists())
            self.assertFalse(ChatMessage.objects.filter(session=self.session, role='assistant').exists())
            for client_id in ('turn-1', 'turn-2'):
                nested = self.post(client_id=client_id)
                self.assertEqual(nested.status_code, 409)
                self.assertEqual(nested.json()['error']['code'], 'MESSAGE_IN_PROGRESS')
            return GOOD_REPLY
        self.responder.side_effect = reply
        self.assertEqual(self.post().status_code, 201)
        self.responder.assert_called_once()
        self.assertEqual(ChatMessage.objects.count(), 2)

    def test_pending_turn_never_reissues_inference_after_restart(self):
        ChatMessage.objects.create(session=self.session, role='user', message_type='text',
                                    content=encrypt('Hôm nay mình thấy áp lực.'),
                                    metadata={'client_message_id': 'turn-1'})
        self.assertEqual(self.post().json()['error']['code'], 'MESSAGE_IN_PROGRESS')
        self.responder.assert_not_called()

    def test_history_is_chronological_and_current_message_appears_once(self):
        self.post(message='Tin đầu tiên', client_id='turn-1')
        self.post(message='Tin thứ hai', client_id='turn-2')
        self.post(message='Tin hiện tại', client_id='turn-3')
        messages = self.payload()['messages']
        self.assertEqual([m['role'] for m in messages], ['system', 'user', 'assistant', 'user', 'assistant', 'user'])
        self.assertEqual(messages[1]['content'], 'Tin đầu tiên')
        self.assertEqual(messages[3]['content'], 'Tin thứ hai')
        self.assertEqual(json.loads(messages[5]['content'])['message'], 'Tin hiện tại')
        self.assertEqual(json.dumps(messages, ensure_ascii=False).count('Tin hiện tại'), 1)

    def test_other_sessions_never_become_raw_chat_context(self):
        self.grant('chat_context', 'ai_chat')
        for owner in (self.user, self.other):
            session = ChatSession.objects.create(user=owner)
            ChatMessage.objects.create(session=session, role='user', message_type='text',
                                        content=encrypt('PRIVATE_OTHER_SESSION'))
        self.post()
        self.assertNotIn('PRIVATE_OTHER_SESSION', json.dumps(self.payload()))

    @override_settings(MINDCARE_CHAT_RESPONDER='mindcare.integrations.esconv.respond',
                       MINDCARE_ESCONV_SPACE_URL='https://test-esconv.hf.space',
                       MINDCARE_ESCONV_API_NAME='generate_chat')
    def test_api_uses_one_gradio_inference_with_default_local_policies(self):
        stream = ('event: complete\ndata: ' + json.dumps([json.dumps(GOOD_REPLY)]) + '\n\n')
        with patch('mindcare.integrations.esconv.urlopen', side_effect=[
            BytesIO(b'{"event_id":"api-turn-1"}'), BytesIO(stream.encode()),
        ]) as opened:
            response = self.post()
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()['data']['assistant_message']['metadata']['status'], 'ok')
            self.assertEqual(self.post().status_code, 201)
        self.assertEqual(opened.call_count, 2)
        self.assertEqual([call.args[0].get_method() for call in opened.call_args_list], ['POST', 'GET'])

    def test_stage_can_return_from_action_to_comforting(self):
        for index, stage in enumerate(('exploration', 'action', 'comforting')):
            self.responder.return_value = {**GOOD_REPLY, 'stage': stage}
            self.assertEqual(self.post(client_id=f'stage-{index}').json()['data']['assistant_message']['metadata']['stage'], stage)
        self.assertEqual(self.responder.call_count, 3)
        self.session.refresh_from_db()
        self.assertEqual(self.session.state_json['last_stage'], 'comforting')

    def test_context_requires_both_context_and_source_consent(self):
        JournalEntry.objects.create(user=self.user, content=encrypt('MY_JOURNAL'), experienced_at=timezone.now())
        JournalEntry.objects.create(user=self.other, content=encrypt('OTHER_JOURNAL'), experienced_at=timezone.now())
        self.grant('ai_journal')
        self.post(client_id='without-context')
        self.assertNotIn('MY_JOURNAL', json.dumps(self.payload()))
        self.grant('chat_context')
        self.post(client_id='with-context')
        self.assertIn('MY_JOURNAL', json.dumps(self.payload()))
        self.assertNotIn('OTHER_JOURNAL', json.dumps(self.payload()))
        UserConsent.objects.create(user=self.user, scope='ai_journal', action='revoke', policy_version='v1')
        self.post(client_id='revoked-context')
        self.assertNotIn('MY_JOURNAL', json.dumps(self.payload()))

    def test_source_revoked_during_inference_discards_draft(self):
        self.grant('chat_context', 'ai_journal')
        JournalEntry.objects.create(user=self.user, content=encrypt('MY_JOURNAL'), experienced_at=timezone.now())
        def revoke(payload):
            UserConsent.objects.create(user=self.user, scope='ai_journal', action='revoke', policy_version='v1')
            return GOOD_REPLY
        self.responder.side_effect = revoke
        response = self.post()
        self.assertEqual(response.status_code, 201)
        metadata = ChatMessage.objects.get(role='assistant').metadata
        self.assertEqual(metadata['fallback_reason'], 'CONTEXT_CHANGED')
        self.assertEqual(metadata['status'], 'degraded')
        self.responder.assert_called_once()

    def test_source_revoked_while_building_context_skips_inference(self):
        self.grant('chat_context', 'ai_journal')
        journal = JournalEntry.objects.create(user=self.user, content=encrypt('MY_JOURNAL'), experienced_at=timezone.now())
        from mindcare.services.chat import context_entries
        def revoke(user):
            facts = context_entries(user)
            journal.content = encrypt('UPDATED_JOURNAL')
            journal.save()
            return facts
        with patch('mindcare.services.chat.context_entries', side_effect=revoke):
            self.assertEqual(self.post().status_code, 201)
        self.responder.assert_not_called()

    def test_outdated_and_revoked_reports_are_excluded(self):
        self.grant('chat_context', 'ai_checkin')
        check = CheckIn.objects.create(user=self.user, checkin_date=timezone.localdate(), recorded_at=timezone.now())
        for state, label in (('completed', 'CURRENT_REPORT'), ('outdated', 'OUTDATED_REPORT')):
            run = AnalysisRun.objects.create(user=self.user, status=state, analysis_type='deep',
                                              period_start=timezone.now() - timedelta(days=7),
                                              period_end=timezone.now(), completed_at=timezone.now())
            AnalysisReport.objects.create(analysis_run=run, summary=label, concern_level='limited')
            AnalysisSource.objects.create(analysis_run=run, check_in=check)
        self.post()
        wire = json.dumps(self.payload())
        self.assertIn('CURRENT_REPORT', wire)
        self.assertNotIn('OUTDATED_REPORT', wire)

    def test_sparse_trend_is_computed_as_missing_without_inventing_zero(self):
        self.grant('chat_context', 'ai_checkin')
        CheckIn.objects.create(user=self.user, checkin_date=timezone.localdate(), recorded_at=timezone.now(), stress_level=8)
        self.post()
        facts = json.loads(self.payload()['messages'][-1]['content'])['context']
        stress = next(f for f in facts if f.get('metric') == 'stress_level')
        self.assertEqual(stress['observed_days'], 1)
        self.assertEqual(stress['recent_average'], 8)
        self.assertIsNone(stress['change'])
        self.assertIsNone(stress['previous_average'])
        self.assertEqual(stress['data_status'], 'insufficient_data')

    def test_invalid_model_results_fallback_without_regeneration(self):
        invalid = [
            'not JSON', '```json\n{}\n```', [],
            {**GOOD_REPLY, 'stage': 'unknown'}, {**GOOD_REPLY, 'strategy': 'unknown'},
            {**GOOD_REPLY, 'response': ''}, {**GOOD_REPLY, 'response': 'x' * 4001},
            {**GOOD_REPLY, 'chain_of_thought': 'PRIVATE_REASONING'},
            {'content': 'legacy format', 'intent': 'support', 'action': 'RESPOND'},
        ]
        for index, value in enumerate(invalid):
            with self.subTest(value=str(value)[:40]):
                self.responder.return_value = value
                self.responder.reset_mock()
                response = self.post(client_id=f'invalid-{index}')
                self.assertEqual(response.status_code, 201)
                metadata = response.json()['data']['assistant_message']['metadata']
                self.assertEqual(metadata, {'stage': None, 'strategy': None, 'status': 'degraded'})
                self.responder.assert_called_once()
        self.session.refresh_from_db()
        self.assertNotIn('last_stage', self.session.state_json)

    def test_timeout_fallback_keeps_last_successful_stage(self):
        self.post()
        self.responder.side_effect = TimeoutError('sensitive provider error')
        response = self.post(client_id='timeout')
        self.assertEqual(response.status_code, 201)
        self.assertNotIn('sensitive provider error', response.content.decode())
        self.session.refresh_from_db()
        self.assertEqual(self.session.state_json['last_stage'], 'comforting')
        self.assertEqual(ChatMessage.objects.filter(role='assistant').latest('created_at').metadata['fallback_reason'], 'MODEL_TIMEOUT')
        self.assertEqual(self.responder.call_count, 2)

    def test_provider_diagnostic_is_preserved_internally(self):
        self.responder.side_effect = ProviderUnavailable('Gradio inference failed.', code='MODEL_OUTPUT_TRUNCATED')
        response = self.post()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['data']['assistant_message']['metadata'],
                         {'stage': None, 'strategy': None, 'status': 'degraded'})
        self.assertEqual(ChatMessage.objects.get(role='assistant').metadata['fallback_reason'], 'MODEL_OUTPUT_TRUNCATED')
        self.assertNotIn('MODEL_OUTPUT_TRUNCATED', response.content.decode())
        self.responder.assert_called_once()

    def test_followup_flags_and_question_text_are_enforced(self):
        for index, (session_allowed, request_allowed, strategy, reply) in enumerate([
            (False, True, 'questions', 'Mình lắng nghe bạn.'),
            (True, False, 'questions', 'Bạn muốn chia sẻ không?'),
            (True, False, 'reflection_of_feelings', 'Bạn có muốn chia sẻ thêm'),
        ]):
            self.session.state_json['followup_enabled'] = session_allowed
            self.session.save()
            self.responder.return_value = {'stage': 'exploration', 'strategy': strategy, 'response': reply}
            response = self.post(client_id=f'followup-{index}', allow_followup_question=request_allowed)
            self.assertIn('DISABLED', self.payload()['messages'][0]['content'])
            self.assertEqual(response.json()['data']['assistant_message']['metadata']['status'], 'degraded')

    def test_followup_disabled_still_allows_support_in_exploration(self):
        self.responder.return_value = {**GOOD_REPLY, 'stage': 'exploration'}
        response = self.post(allow_followup_question=False)
        self.assertEqual(response.json()['data']['assistant_message']['metadata']['status'], 'ok')

    def test_output_safety_blocks_diagnosis_without_another_llm(self):
        self.responder.return_value = {**GOOD_REPLY, 'response': 'Bạn chắc chắn mắc bệnh trầm cảm.'}
        response = self.post()
        self.assertEqual(response.json()['data']['assistant_message']['metadata']['status'], 'degraded')
        self.assertNotIn('mắc bệnh', response.json()['data']['assistant_message']['content'])
        self.responder.assert_called_once()

    def test_unsubstantiated_personal_trend_is_blocked(self):
        self.responder.return_value = {**GOOD_REPLY, 'response': 'Stress của bạn đã tăng 99% trong tuần này.'}
        self.assertEqual(self.post().json()['data']['assistant_message']['metadata']['status'], 'degraded')
        self.responder.assert_called_once()

    def test_followup_revoked_while_model_is_running(self):
        def disable(payload):
            self.session.state_json = {**self.session.state_json, 'followup_enabled': False}
            self.session.save()
            return {'stage': 'exploration', 'strategy': 'questions', 'response': 'Bạn muốn chia sẻ thêm không?'}
        self.responder.side_effect = disable
        self.assertEqual(self.post().json()['data']['assistant_message']['metadata']['status'], 'degraded')
        self.responder.assert_called_once()

    def test_urgent_signal_skips_model_and_does_not_deliver_contact(self):
        response = self.post(message='Tôi muốn tự tử ngay bây giờ.')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['data']['assistant_message']['metadata'],
                         {'stage': None, 'strategy': None, 'status': 'safety_override'})
        self.responder.assert_not_called()
        alert = SafetyAlert.objects.get(user=self.user)
        self.assertEqual(alert.status, 'pending_review')
        self.assertEqual(alert.severity, 'urgent')
        self.assertNotIn('Tin nhắn', alert.reason)
        self.assertFalse(AlertDelivery.objects.exists())

    @override_settings(MINDCARE_SAFETY_DETECTOR='')
    def test_missing_safety_policy_rolls_back_turn(self):
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'SAFETY_DETECTOR_UNAVAILABLE')
        self.assertFalse(ChatMessage.objects.exists())
        self.responder.assert_not_called()

    @override_settings(MINDCARE_CHAT_MAX_INPUT_TOKENS=32)
    def test_oversized_prompt_is_rejected_before_saving_or_inference(self):
        response = self.post()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['code'], 'CONTEXT_TOO_LONG')
        self.assertFalse(ChatMessage.objects.exists())
        self.responder.assert_not_called()

    def test_history_is_trimmed_in_complete_pairs(self):
        for index in range(8):
            message = ChatMessage.objects.create(session=self.session, role='user', message_type='text',
                                                 content=encrypt(f'OLD-{index} ' + 'a' * 600))
            ChatMessage.objects.create(session=self.session, reply_to_message=message, role='assistant',
                                        message_type='text', content=encrypt('b' * 600),
                                        metadata={'pipeline_version': PIPELINE_VERSION})
        self.post(message='CURRENT')
        messages = self.payload()['messages']
        wire = json.dumps(messages)
        self.assertLessEqual(chat_contract.input_token_upper_bound(messages), 8192)
        self.assertNotIn('OLD-0', wire)
        self.assertIn('OLD-7', wire)
        self.assertEqual(json.loads(messages[-1]['content'])['message'], 'CURRENT')
        chat_contract.validate_wire_payload(self.payload())

    def test_client_cannot_override_system_prompt_or_strategy(self):
        for field in ('stage', 'strategy', 'history', 'system_prompt', 'provider'):
            self.assertEqual(self.post(**{field: 'injected'}).status_code, 422)
        self.assertEqual(self.post(message=' ').json()['error']['code'], 'EMPTY_MESSAGE')
        self.responder.assert_not_called()

    def test_ownership_is_checked_before_inference(self):
        session = ChatSession.objects.create(user=self.other)
        self.path = f'/api/v1/me/chat/sessions/{session.pk}/messages'
        self.assertEqual(self.post().status_code, 404)
        self.responder.assert_not_called()

    def test_feedback_preserves_esconv_labels_and_prior_fields(self):
        response = self.post().json()['data']['assistant_message']
        path = f"/api/v1/me/chat/messages/{response['id']}/feedback"
        for payload in ({'helpful': True}, {'recommendation_status': 'saved'}):
            self.assertEqual(self.client.patch(path, data=json.dumps(payload), content_type='application/json',
                                               **self.headers).status_code, 200)
        stored = ChatMessage.objects.get(pk=response['id'])
        self.assertEqual(stored.metadata['strategy'], 'reflection_of_feelings')
        self.assertTrue(stored.metadata['feedback']['helpful'])
        self.assertEqual(stored.metadata['feedback']['recommendation_status'], 'saved')
        self.responder.assert_called_once()

    def test_checkin_proposal_is_rule_based_and_requires_confirmation(self):
        self.post(message='Mình ngủ 5 giờ và stress 8/10.')
        message = ChatMessage.objects.get(role='user')
        self.assertEqual(message.metadata['extracted_checkin_proposal']['sleep_hours'], 5)
        self.assertEqual(message.metadata['extracted_checkin_proposal']['stress_level'], 8)
        self.assertFalse(CheckIn.objects.exists())
        self.responder.assert_called_once()

    def test_checkin_proposal_respects_explicit_dates_and_skips_ambiguous_time(self):
        from mindcare.services.chat import checkin_proposal
        self.assertEqual(checkin_proposal('Ngày 2026-01-01 mình ngủ 5 giờ.', self.user)['checkin_date'], '2026-01-01')
        self.assertIsNone(checkin_proposal('Tuần trước mình ngủ 5 giờ.', self.user))
        self.assertIsNone(checkin_proposal('Ngày 9999-01-01 mình ngủ 5 giờ.', self.user))

    def test_analysis_worker_rechecks_consent(self):
        self.post()
        with patch('mindcare.services.light_analysis.LightAnalysisService.analyze_chat_batch') as analyze:
            result = process_chat_light_analysis(str(ChatMessage.objects.get(role='user').pk))
        self.assertEqual(result['status'], 'CONSENT_REQUIRED')
        analyze.assert_not_called()


@override_settings(MINDCARE_DATA_ENCRYPTION_KEY=TEST_KEY,
                   MINDCARE_SAFETY_DETECTOR='mindcare.services.chat_safety.detect_safety',
                   MINDCARE_CHAT_OUTPUT_GUARD='mindcare.services.chat_safety.guard_output',
                   MINDCARE_CHAT_MAX_INPUT_TOKENS=8192)
class ChatQueueTests(TransactionTestCase):
    def test_enqueue_occurs_after_saved_reply_and_retry_preserves_ack(self):
        from mindcare.services.chat import send_message
        user = User.objects.create(email='queue@example.com', role='user', status='active', timezone='UTC')
        session = ChatSession.objects.create(user=user)
        UserConsent.objects.create(user=user, scope='ai_chat', action='grant', policy_version='v1')
        def enqueue(message_id):
            from django.db import connection
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(ChatMessage.objects.filter(reply_to_message_id=message_id).exists())
        queued = Mock(side_effect=enqueue)
        responder = Mock(return_value=GOOD_REPLY)
        with override_settings(MINDCARE_CHAT_ANALYSIS_ENQUEUE=queued, MINDCARE_CHAT_RESPONDER=responder):
            first = send_message(user, session.pk, 'Xin chào', 'queued-turn', True)
            self.assertTrue(first['analysis_queued'])
            self.assertEqual(first, send_message(user, session.pk, 'Xin chào', 'queued-turn', True))
        queued.assert_called_once()
        responder.assert_called_once()


@override_settings(MINDCARE_ESCONV_SPACE_URL='https://test-esconv.hf.space',
                   MINDCARE_ESCONV_API_NAME='generate_chat', MINDCARE_ESCONV_TIMEOUT_SECONDS=5,
                   MINDCARE_ESCONV_HF_TOKEN='test-hf-token')
class GradioTransportTests(SimpleTestCase):
    def payload(self):
        return {'messages': chat_contract.format_messages('Xin chào', True), 'prompt_version': PROMPT_VERSION}

    def test_one_submit_and_sse_completion(self):
        stream = ('event: heartbeat\ndata: null\n\nevent: generating\ndata: ["partial"]\n\n'
                  + 'event: complete\ndata: ' + json.dumps([json.dumps(GOOD_REPLY)]) + '\n\n')
        with patch('mindcare.integrations.esconv.urlopen', side_effect=[
            BytesIO(b'{"event_id":"event-123"}'), BytesIO(stream.encode()),
        ]) as opened:
            self.assertEqual(parse_result(space_respond(self.payload())), GOOD_REPLY)
        self.assertEqual(opened.call_count, 2)
        submit, poll = [call.args[0] for call in opened.call_args_list]
        self.assertEqual(submit.get_method(), 'POST')
        self.assertTrue(submit.full_url.endswith('/gradio_api/call/generate_chat'))
        self.assertEqual(poll.get_method(), 'GET')
        self.assertTrue(poll.full_url.endswith('/generate_chat/event-123'))
        self.assertEqual(submit.get_header('Authorization'), 'Bearer test-hf-token')
        self.assertEqual(json.loads(json.loads(submit.data)['data'][0]), self.payload())

    def test_gradio_error_does_not_submit_again(self):
        with patch('mindcare.integrations.esconv.urlopen', side_effect=[
            BytesIO(b'{"event_id":"error-123"}'), BytesIO(b'event: error\ndata: "private raw error"\n\n'),
        ]) as opened:
            with self.assertRaises(ProviderUnavailable) as caught:
                space_respond(self.payload())
        self.assertNotIn('private raw error', str(caught.exception))
        self.assertEqual(opened.call_count, 2)

    def test_structured_space_error_is_allowlisted(self):
        for value, expected in (
            ('ESCONV_MODEL_OUTPUT_TRUNCATED', 'MODEL_OUTPUT_TRUNCATED'),
            ({'error': 'ESCONV_GPU_MEMORY_EXHAUSTED'}, 'GPU_MEMORY_EXHAUSTED'),
            ('ESCONV_UNKNOWN_PRIVATE_TEXT', 'MODEL_UNAVAILABLE'),
        ):
            with self.subTest(value=value):
                stream = 'event: error\ndata: ' + json.dumps(value) + '\n\n'
                with patch('mindcare.integrations.esconv.urlopen', side_effect=[
                    BytesIO(b'{"event_id":"error-123"}'), BytesIO(stream.encode()),
                ]) as opened:
                    with self.assertRaises(ProviderUnavailable) as caught:
                        space_respond(self.payload())
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn('UNKNOWN_PRIVATE_TEXT', str(caught.exception))
                self.assertEqual(opened.call_count, 2)

    def test_timeout_never_resubmits(self):
        with patch('mindcare.integrations.esconv.urlopen', side_effect=TimeoutError) as opened:
            with self.assertRaises(TimeoutError):
                space_respond(self.payload())
        opened.assert_called_once()

    def test_malformed_event_id_cannot_change_result_url(self):
        with patch('mindcare.integrations.esconv.urlopen', return_value=BytesIO(b'{"event_id":"../../secret"}')) as opened:
            with self.assertRaises(ProviderUnavailable):
                space_respond(self.payload())
        opened.assert_called_once()

    def test_incomplete_stream_is_not_success(self):
        with patch('mindcare.integrations.esconv.urlopen', side_effect=[
            BytesIO(b'{"event_id":"event-123"}'), BytesIO(b'event: heartbeat\ndata: null\n\n'),
        ]):
            with self.assertRaises(ProviderUnavailable):
                space_respond(self.payload())


class ChatPolicyAndSpaceTests(SimpleTestCase):
    def test_rules_distinguish_explicit_signal_and_ordinary_discussion(self):
        for text in ('Hôm nay mình stress quá.', 'Tự tử nghĩa là gì?', 'Tôi không muốn tự tử.'):
            self.assertIsNone(detect_safety(text))
        self.assertEqual(detect_safety('Tôi muốn tự tử ngay bây giờ.')['severity'], 'urgent')
        self.assertEqual(detect_safety('I want to hurt myself.')['severity'], 'needs_review')
        self.assertFalse(guard_output('Bạn chắc chắn mắc bệnh trầm cảm.'))
        self.assertFalse(guard_output('Mình đã gửi cảnh báo cho gia đình.'))
        self.assertTrue(guard_output(GOOD_REPLY['response']))

    def test_prepared_bundle_has_shared_contract_and_compiles(self):
        with tempfile.TemporaryDirectory() as directory:
            call_command('prepare_esconv_space', output=Path(directory), stdout=StringIO())
            names = {p.name for p in Path(directory).iterdir()}
            self.assertEqual(names, {'app.py', 'chat_contract.py', 'README.md', 'requirements.txt'})
            for name in ('app.py', 'chat_contract.py'):
                compile((Path(directory) / name).read_text(), name, 'exec')
            self.assertEqual((Path(directory) / 'chat_contract.py').read_text(),
                             (settings.BASE_DIR / 'mindcare' / 'chat_contract.py').read_text())

    def space_module(self):
        fake_torch = MagicMock()
        fake_torch.inference_mode.return_value = nullcontext()
        fake_spaces = MagicMock()
        fake_spaces.GPU = lambda **kwargs: lambda function: function
        modules = {name: MagicMock() for name in ('gradio', 'peft', 'transformers', 'lmformatenforcer',
                                                 'lmformatenforcer.integrations', 'lmformatenforcer.integrations.transformers')}
        class GradioError(Exception):
            pass
        modules['gradio'].Error = GradioError
        modules.update(torch=fake_torch, spaces=fake_spaces, chat_contract=chat_contract)
        spec = importlib.util.spec_from_file_location('test_space_app', settings.BASE_DIR / 'deploy/huggingface_space/app.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', modules):
            spec.loader.exec_module(module)
        return module, fake_torch

    def test_space_generates_once_including_on_invalid_output(self):
        module, fake_torch = self.space_module()
        tokenizer, model, tensor = MagicMock(), MagicMock(), MagicMock()
        tensor.shape = (1, 20)
        tensor.to.return_value = tensor
        tokenizer.apply_chat_template.return_value = {'input_ids': tensor, 'attention_mask': tensor}
        tokenizer.eos_token_id = 2
        tokenizer.pad_token_id = 2
        tokenizer.convert_tokens_to_ids.return_value = 3
        model.config.max_position_embeddings = 131072
        payload = {'messages': chat_contract.format_messages('Xin chào', True), 'prompt_version': PROMPT_VERSION}
        for text, output_tokens, expected_error in (
            (json.dumps(GOOD_REPLY), 1, None),
            ('invalid JSON', 1, 'INVALID_MODEL_OUTPUT'),
            ('unfinished JSON', module.MAX_NEW_TOKENS, 'MODEL_OUTPUT_TRUNCATED'),
        ):
            model.generate.reset_mock()
            model.generate.return_value = [[0] * 20 + [1] * output_tokens]
            fake_torch.inference_mode.return_value = nullcontext()
            tokenizer.decode.return_value = text
            if expected_error is None:
                with self.assertLogs('mindcare.esconv', level='INFO') as logs:
                    self.assertEqual(json.loads(module.generate_once(payload, tokenizer, model)), GOOD_REPLY)
                logged = '\n'.join(logs.output)
                for field in ('input_tokens=20', 'output_tokens=1', 'prepare_seconds=',
                              'generate_seconds=', 'total_seconds='):
                    self.assertIn(field, logged)
                self.assertNotIn(GOOD_REPLY['response'], logged)
                self.assertNotIn('Xin chào', logged)
            else:
                with self.assertRaises(SpaceInferenceError) as caught:
                    module.generate_once(payload, tokenizer, model)
                self.assertEqual(caught.exception.code, expected_error)
            model.generate.assert_called_once()
            self.assertIn('prefix_allowed_tokens_fn', model.generate.call_args.kwargs)
            self.assertIn('generation_config', model.generate.call_args.kwargs)

    def test_space_initializes_in_parent_and_reuses_runtime_across_requests(self):
        module, _ = self.space_module()
        tokenizer, model, decoder = object(), object(), object()
        payload = {'messages': chat_contract.format_messages('Xin chào', True),
                   'prompt_version': PROMPT_VERSION}
        with patch.object(module, 'load_engine', return_value=(tokenizer, model)) as load:
            with patch.object(module, 'build_token_enforcer_tokenizer_data', return_value=decoder) as build:
                with self.assertLogs('mindcare.esconv', level='INFO') as logs:
                    module.initialize_runtime()
                    module.initialize_runtime()
                with patch.object(module, 'generate_once', return_value=json.dumps(GOOD_REPLY)) as generate:
                    for _ in range(2):
                        self.assertEqual(json.loads(module.generate_chat(json.dumps(payload))), GOOD_REPLY)
        load.assert_called_once_with()
        build.assert_called_once_with(tokenizer)
        self.assertEqual(generate.call_count, 2)
        generate.assert_called_with(payload, tokenizer, model, decoder)
        self.assertIn('esconv_ready', '\n'.join(logs.output))

    def test_space_reads_adapter_on_cpu_before_gpu_is_allocated(self):
        module, _ = self.space_module()
        model = MagicMock()

        def read_adapter(base, adapter, **kwargs):
            # Safetensors' default CUDA reader bypasses ZeroGPU's startup
            # emulation. Reproduce that boundary instead of allowing any call.
            if kwargs.get('torch_device') != 'cpu':
                raise RuntimeError('No CUDA GPUs are available')
            return model

        with patch.dict(module.os.environ, {'HF_TOKEN': 'FAKE_PRIVATE_HF_TOKEN'}):
            with patch.object(module.PeftModel, 'from_pretrained', side_effect=read_adapter) as load:
                with self.assertLogs('mindcare.esconv', level='INFO') as logs:
                    module.initialize_runtime()
        self.assertIs(module._runtime[1], model)
        load.assert_called_once()
        self.assertFalse(load.call_args.kwargs['is_trainable'])
        self.assertIn('esconv_load phase=adapter', '\n'.join(logs.output))
        self.assertIn('esconv_ready', '\n'.join(logs.output))
        self.assertNotIn('FAKE_PRIVATE_HF_TOKEN', '\n'.join(logs.output))

    def test_space_startup_failure_reports_code_without_private_exception_text(self):
        for phase, code in (('load_model', 'MODEL_LOAD_FAILED'), ('decoder_setup', 'DECODER_SETUP_FAILED')):
            module, _ = self.space_module()
            with patch.object(module, 'load_engine', return_value=(object(), object())) as load:
                with patch.object(module, 'build_token_enforcer_tokenizer_data') as build:
                    failing = load if phase == 'load_model' else build
                    failing.side_effect = RuntimeError('PRIVATE_TOKEN_AND_PROMPT')
                    with self.assertLogs('mindcare.esconv', level='ERROR') as logs:
                        with self.assertRaises(RuntimeError) as caught:
                            module.initialize_runtime()
            self.assertEqual(str(caught.exception), f'ESCONV_{code}')
            self.assertIsNone(module._runtime)
            self.assertIn('location=', '\n'.join(logs.output))
            self.assertIn('initialize_runtime', '\n'.join(logs.output))
            self.assertNotIn('PRIVATE_TOKEN_AND_PROMPT', '\n'.join(logs.output))

    def test_space_uninitialized_worker_never_lazy_loads_model(self):
        module, _ = self.space_module()
        payload = json.dumps({'messages': chat_contract.format_messages('Xin chào', True),
                              'prompt_version': PROMPT_VERSION})
        with patch.object(module, 'load_engine') as load:
            with self.assertLogs('mindcare.esconv', level='ERROR'):
                with self.assertRaises(module.gr.Error) as caught:
                    module.generate_chat(payload)
        self.assertEqual(str(caught.exception), 'ESCONV_MODEL_UNAVAILABLE')
        load.assert_not_called()

    def test_space_inference_failure_reports_code_without_private_exception_text(self):
        module, _ = self.space_module()
        module._runtime = object(), object(), object()
        payload = json.dumps({'messages': chat_contract.format_messages('Xin chào', True),
                              'prompt_version': PROMPT_VERSION})
        with patch.object(module, 'generate_once', side_effect=RuntimeError('PRIVATE_TOKEN_AND_PROMPT')):
            with self.assertLogs('mindcare.esconv', level='ERROR') as logs:
                with self.assertRaises(module.gr.Error) as caught:
                    module.generate_chat(payload)
        self.assertEqual(str(caught.exception), 'ESCONV_MODEL_GENERATION_FAILED')
        self.assertNotIn('PRIVATE_TOKEN_AND_PROMPT', '\n'.join(logs.output))

    def test_decoder_logs_never_print_the_token_prefix(self):
        import logging
        module, _ = self.space_module()
        record = logging.LogRecord('root', logging.ERROR, '/site-packages/lmformatenforcer/tokenenforcer.py',
                                    119, 'Prefix: %s', ('PRIVATE_PROMPT',),
                                    (ValueError, ValueError('PRIVATE_EXCEPTION'), None))
        module.DecoderLogFilter().filter(record)
        self.assertNotIn('PRIVATE', record.getMessage())
        self.assertIsNone(record.exc_info)

    def test_swagger_exposes_the_chat_contract(self):
        schema = build_openapi_schema(None)
        operation = schema['paths']['/me/chat/sessions/{session_id}/messages']['post']
        self.assertIn('201', operation['responses'])
        self.assertIn('409', operation['responses'])
        self.assertNotIn('200', operation['responses'])
        metadata = schema['components']['schemas']['ChatMetadata']['properties']
        self.assertEqual(len(metadata['stage']['enum']), 4)  # Three stages + fallback null.
        self.assertEqual(len(metadata['strategy']['enum']), 9)  # Eight labels + fallback null.
