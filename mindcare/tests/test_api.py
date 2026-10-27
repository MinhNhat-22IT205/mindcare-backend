import json
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.hashers import make_password
from django.conf import settings
from django.core.management import call_command
from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django.urls import resolve

from mindcare.api.common import encrypt, token_for
from mindcare.api.swagger import build_openapi_schema
from mindcare.models import (AnalysisReport, AnalysisRun, AnalysisSource, CheckIn,
                             ChatMessage, JournalEntry, SafetyAlert, TrustedContact, User)
from mindcare.services.deep_analysis import DeepAnalysisService
from mindcare.services.alert_delivery import deliver_queued_alert


@override_settings(MINDCARE_DATA_ENCRYPTION_KEY='MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=',
                   MINDCARE_SAFETY_DETECTOR='mindcare.tests.test_api.no_signal')
class APITests(TestCase):
    def setUp(self):
        self.user = User.objects.create(email='a@example.com', display_name='A',
            password_hash=make_password('long-test-password-A!'), role='user',
            status='active', timezone='UTC')
        self.other = User.objects.create(email='b@example.com', display_name='B',
            password_hash=make_password('long-test-password-B!'), role='user',
            status='active', timezone='UTC')
        self.support = User.objects.create(email='s@example.com', display_name='S',
            password_hash=make_password('long-test-password-S!'), role='support',
            status='active', timezone='UTC')
        self.admin = User.objects.create(email='admin@example.com', display_name='Admin',
            password_hash=make_password('long-test-password-Z!'), role='admin',
            status='active', timezone='UTC')

    def call(self, method, path, payload=None, user=None):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {token_for(user)}'} if user else {}
        if method in {'get', 'delete'}:
            return getattr(self.client, method)(path, **headers)
        return getattr(self.client, method)(path, data=json.dumps(payload or {}),
                                             content_type='application/json', **headers)

    def test_register_login_and_role(self):
        response = self.call('post', '/api/v1/auth/register', {
            'email': ' NEW@Example.com ', 'password': 'a-long-strong-test-password!22',
            'display_name': 'New', 'timezone': 'UTC', 'role': 'admin'})
        self.assertEqual(response.status_code, 422)
        response = self.call('post', '/api/v1/auth/register', {
            'email': ' NEW@Example.com ', 'password': 'a-long-strong-test-password!22',
            'display_name': 'New', 'timezone': 'UTC'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(User.objects.get(email='new@example.com').role, 'user')
        response = self.call('post', '/api/v1/auth/login', {
            'email': 'new@example.com', 'password': 'a-long-strong-test-password!22'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('access_token', response.json()['data'])
        self.assertEqual(self.call('get', '/api/v1/me').status_code, 401)

    def test_cross_user_is_hidden_and_inactive_token_fails(self):
        check = CheckIn.objects.create(user=self.other, checkin_date=timezone.localdate(),
            recorded_at=timezone.now(), stress_level=6)
        self.assertEqual(self.call('get', f'/api/v1/me/checkins/{check.id}', user=self.user).status_code, 404)
        token = token_for(self.user)
        self.user.status = 'inactive'
        self.user.save()
        response = self.client.get('/api/v1/me', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(response.status_code, 403)

    def test_checkin_null_duplicate_and_outdated(self):
        day = timezone.localdate().isoformat()
        response = self.call('post', '/api/v1/me/checkins', {'checkin_date': day, 'stress_level': 7}, self.user)
        self.assertEqual(response.status_code, 201)
        check = CheckIn.objects.get(user=self.user)
        self.assertIsNone(check.mood_level)
        series = self.call('get', '/api/v1/me/dashboard', user=self.user).json()['data']['series']
        self.assertEqual(len(series), 14)
        self.assertEqual(sum(row['observed'] for row in series), 1)
        self.assertIsNone(series[0]['stress_level'])
        self.assertEqual(self.call('post', '/api/v1/me/checkins', {
            'checkin_date': day, 'stress_level': 7}, self.user).status_code, 409)
        run = AnalysisRun.objects.create(user=self.user, analysis_type='deep', status='completed',
            period_start=timezone.now() - timedelta(days=7), period_end=timezone.now())
        AnalysisSource.objects.create(analysis_run=run, check_in=check)
        response = self.call('patch', f'/api/v1/me/checkins/{check.id}', {'stress_level': 5}, self.user)
        self.assertEqual(response.status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.status, 'outdated')

    def test_journal_ciphertext_and_delete_report(self):
        response = self.call('post', '/api/v1/me/journals', {'content': 'Very private journal'}, self.user)
        self.assertEqual(response.status_code, 201)
        journal = JournalEntry.objects.get(user=self.user)
        self.assertNotIn('Very private journal', journal.content)
        self.assertEqual(self.call('get', f'/api/v1/me/journals/{journal.id}', user=self.user).json()['data']['content'],
                         'Very private journal')
        run = AnalysisRun.objects.create(user=self.user, analysis_type='deep', status='completed',
            period_start=timezone.now() - timedelta(days=7), period_end=timezone.now())
        AnalysisSource.objects.create(analysis_run=run, journal=journal)
        AnalysisReport.objects.create(analysis_run=run, concern_level='limited', summary='old')
        self.assertEqual(self.call('delete', f'/api/v1/me/journals/{journal.id}', user=self.user).status_code, 204)
        self.assertFalse(AnalysisReport.objects.filter(analysis_run=run).exists())

    def test_support_assignment_and_admin_privacy(self):
        alert = SafetyAlert.objects.create(user=self.user, severity='needs_review',
            reason=encrypt('User requested help'), status='pending_review', trigger_type='user_request')
        self.assertEqual(self.call('get', '/api/v1/support/alerts', user=self.user).status_code, 403)
        self.assertEqual(self.call('get', f'/api/v1/support/alerts/{alert.id}', user=self.support).status_code, 404)
        response = self.call('post', f'/api/v1/admin/alerts/{alert.id}/assign',
                             {'reviewer_user_id': str(self.support.id)}, self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.call('get', f'/api/v1/support/alerts/{alert.id}', user=self.support).status_code, 200)
        users = self.call('get', '/api/v1/admin/users', user=self.admin).json()['data']['items']
        self.assertNotIn('password_hash', users[0])
        self.assertEqual(self.call('patch', f'/api/v1/admin/users/{self.admin.id}',
                                   {'status': 'inactive'}, self.admin).status_code, 409)

    def test_analysis_request_does_not_create_stuck_queue(self):
        response = self.call('post', '/api/v1/me/consents', {
            'scope': 'ai_checkin', 'action': 'grant', 'policy_version': 'v1'}, self.user)
        self.assertEqual(response.status_code, 201)
        response = self.call('post', '/api/v1/me/analysis-runs', {
            'analysis_type': 'deep', 'period_start': (timezone.now() - timedelta(days=7)).isoformat(),
            'period_end': timezone.now().isoformat()}, self.user)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(AnalysisRun.objects.filter(user=self.user).exists())

    @override_settings(MINDCARE_ANALYSIS_ENQUEUE='mindcare.tests.test_api.accept_job')
    def test_deep_worker_uses_queued_run(self):
        self.call('post', '/api/v1/me/consents', {
            'scope': 'ai_checkin', 'action': 'grant', 'policy_version': 'v1'}, self.user)
        CheckIn.objects.create(user=self.user, checkin_date=timezone.localdate(),
                               recorded_at=timezone.now() - timedelta(hours=1), stress_level=5)
        response = self.call('post', '/api/v1/me/analysis-runs', {
            'analysis_type': 'deep', 'period_start': (timezone.now() - timedelta(days=7)).isoformat(),
            'period_end': timezone.now().isoformat()}, self.user)
        self.assertEqual(response.status_code, 202)
        run_id = response.json()['data']['run_id']
        DeepAnalysisService.run_for_queued(run_id, analyzer=lambda _context: {
            'emotion_summary': {}, 'concern_level': 'insufficient_data',
            'summary': 'Không đủ dữ liệu.', 'recommendations': []})
        self.assertEqual(AnalysisRun.objects.get(id=run_id).status, 'completed')
        self.assertEqual(self.call('get', f'/api/v1/me/analysis-runs/{run_id}/report',
                                   user=self.user).status_code, 200)

    def test_chat_idempotency_and_ciphertext(self):
        session = self.call('post', '/api/v1/me/chat/sessions', {'title': 'Hôm nay'}, self.user).json()['data']
        path = f"/api/v1/me/chat/sessions/{session['id']}/messages"
        payload = {'message': 'Hôm nay mình khá mệt.', 'client_message_id': 'client-1'}
        first = self.call('post', path, payload, self.user)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self.call('post', path, payload, self.user).status_code, 201)
        self.assertEqual(ChatMessage.objects.filter(session_id=session['id']).count(), 2)
        self.assertNotIn(payload['message'], ChatMessage.objects.get(role='user').content)

    def test_report_hidden_after_consent_revoked(self):
        self.call('post', '/api/v1/me/consents', {
            'scope': 'ai_checkin', 'action': 'grant', 'policy_version': 'v1'}, self.user)
        check = CheckIn.objects.create(user=self.user, checkin_date=timezone.localdate(),
            recorded_at=timezone.now(), stress_level=5)
        run = AnalysisRun.objects.create(user=self.user, analysis_type='deep', status='completed',
            period_start=timezone.now() - timedelta(days=7), period_end=timezone.now())
        AnalysisSource.objects.create(analysis_run=run, check_in=check)
        AnalysisReport.objects.create(analysis_run=run, concern_level='limited', summary='Private')
        self.assertEqual(self.call('get', f'/api/v1/me/analysis-runs/{run.id}/report',
                                   user=self.user).status_code, 200)
        self.call('post', '/api/v1/me/consents', {
            'scope': 'ai_checkin', 'action': 'revoke', 'policy_version': 'v1'}, self.user)
        self.assertEqual(self.call('get', f'/api/v1/me/analysis-runs/{run.id}/report',
                                   user=self.user).status_code, 403)

    def test_legacy_encryption_command(self):
        journal = JournalEntry.objects.create(user=self.user, content='Legacy private text',
                                              experienced_at=timezone.now())
        call_command('encrypt_legacy_data', verbosity=0)
        journal.refresh_from_db()
        self.assertNotIn('Legacy private text', journal.content)
        self.assertEqual(self.call('get', f'/api/v1/me/journals/{journal.id}',
                                   user=self.user).json()['data']['content'], 'Legacy private text')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                       MINDCARE_PASSWORD_RESET_URL='https://example.com/reset')
    def test_password_reset_invalidates_previous_jwt(self):
        old_token = token_for(self.user)
        response = self.call('post', '/api/v1/auth/password-reset/request',
                             {'email': self.user.email})
        self.assertEqual(response.status_code, 200)
        query = parse_qs(urlparse(mail.outbox[-1].body).query)
        response = self.call('post', '/api/v1/auth/password-reset/confirm', {
            'uid': query['uid'][0], 'token': query['token'][0],
            'new_password': 'brand-new-secure-test-password!42'})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get('/api/v1/me',
            HTTP_AUTHORIZATION=f'Bearer {old_token}').status_code, 401)

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                       MINDCARE_CONTACT_INVITATION_URL='https://example.com/invite',
                       MINDCARE_ALERT_DELIVERY_ENQUEUE='mindcare.tests.test_api.accept_job')
    def test_contact_requires_verification_and_consent_for_delivery(self):
        response = self.call('post', '/api/v1/me/trusted-contacts', {
            'display_name': 'Family', 'relationship': 'family',
            'contact_type': 'email', 'contact_value': 'family@example.com'}, self.user)
        self.assertEqual(response.status_code, 201)
        contact_id = response.json()['data']['id']
        response = self.call('post', f'/api/v1/me/trusted-contacts/{contact_id}/verification-request',
                             {}, self.user)
        self.assertEqual(response.status_code, 202)
        token = parse_qs(urlparse(mail.outbox[-1].body).query)['token'][0]
        self.assertEqual(self.call('post', '/api/v1/trusted-contact-invitations/accept',
                                   {'token': token}).status_code, 204)
        self.assertEqual(TrustedContact.objects.get(id=contact_id).verification_status, 'verified')
        alert = SafetyAlert.objects.create(user=self.user, severity='needs_review',
            reason=encrypt('Please contact me'), status='pending_review', trigger_type='user_request',
            reviewer_user=self.support)
        response = self.call('post', f'/api/v1/support/alerts/{alert.id}/review', {
            'decision': 'approve', 'reason': 'Verified request'}, self.support)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['deliveries_queued'], 0)
        self.call('post', '/api/v1/me/consents', {'scope': f'share_alert_contact:{contact_id}',
            'action': 'grant', 'policy_version': 'v1'}, self.user)
        second = SafetyAlert.objects.create(user=self.user, severity='needs_review',
            reason=encrypt('Please contact me'), status='pending_review', trigger_type='user_request',
            reviewer_user=self.support)
        response = self.call('post', f'/api/v1/support/alerts/{second.id}/review', {
            'decision': 'approve', 'reason': 'Verified request'}, self.support)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['deliveries_queued'], 1)
        sent = []
        deliver_queued_alert(second.id, sender=lambda **kwargs: sent.append(kwargs))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['recipient'], 'family@example.com')


class RouteContractTests(SimpleTestCase):
    def test_every_documented_api_resolves(self):
        doc = (Path(settings.BASE_DIR) / 'API_Doc.md').read_text(encoding='utf-8')
        overview = doc.split('## 6. Đặc tả API chi tiết')[0]
        routes = re.findall(r'API-\d+.*?\*\*(GET|POST|PATCH|PUT|DELETE)\*\*.*?(/(?:auth|me|support|admin|trusted-contact)[^ `|]*)', overview)
        self.assertEqual(len(routes), 67)
        for method, path in routes:
            sample = re.sub(r'\{\w+\}', '11111111-1111-4111-8111-111111111111', path)
            with self.subTest(method=method, path=path):
                self.assertIn(method, resolve('/api/v1' + sample).func.allowed_methods)

    def test_swagger_documents_every_operation_with_prefilled_writes(self):
        schema = build_openapi_schema()
        operations = [
            operation
            for path_item in schema['paths'].values()
            for method, operation in path_item.items()
            if method in {'get', 'post', 'put', 'patch', 'delete'}
        ]
        self.assertEqual(len(operations), 67)
        for operation in operations:
            method = operation['operationId'].split('_', 1)[0]
            if method in {'post', 'put', 'patch'}:
                media = operation['requestBody']['content']['application/json']
                self.assertIn('example', media)

    def test_swagger_pages_are_available(self):
        response = self.client.get('/api/v1/openapi.json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['openapi'], '3.0.3')
        response = self.client.get('/api/v1/docs/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Tạo/đăng nhập tài khoản test')
        self.assertContains(response, 'tryItOutEnabled: true')


def no_signal(_text):
    return None


def accept_job(_run_id):
    return None
