import uuid

from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class User(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    display_name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    password_hash = models.CharField(max_length=255, blank=True)
    role = models.CharField(max_length=32)
    status = models.CharField(max_length=32)
    timezone = models.CharField(max_length=64)
    reminder_enabled = models.BooleanField(default=False)
    reminder_time = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'users'
        constraints = [models.UniqueConstraint(Lower('email'), name='users_email_lower_unique')]


class UserConsent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='consents')
    scope = models.CharField(max_length=64)
    action = models.CharField(max_length=32)
    policy_version = models.CharField(max_length=64)
    recorded_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'user_consents'
        indexes = [models.Index(fields=['user', 'scope', '-recorded_at'], name='consents_user_scope_time')]


class CheckIn(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='check_ins')
    checkin_date = models.DateField()
    stress_level = models.IntegerField(null=True, blank=True)
    mood_level = models.IntegerField(null=True, blank=True)
    anxiety_level = models.IntegerField(null=True, blank=True)
    energy_level = models.IntegerField(null=True, blank=True)
    sleep_hours = models.FloatField(null=True, blank=True)
    note = models.TextField(blank=True)
    recorded_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'check_ins'
        constraints = [
            models.UniqueConstraint(fields=['user', 'checkin_date'], name='checkins_user_date_unique'),
            *[
                models.CheckConstraint(
                    condition=Q(**{f'{field}__gte': 0, f'{field}__lte': 10}) | Q(**{f'{field}__isnull': True}),
                    name=f'checkins_{field}_range',
                )
                for field in ('stress_level', 'mood_level', 'anxiety_level', 'energy_level')
            ],
            models.CheckConstraint(
                condition=Q(sleep_hours__gte=0, sleep_hours__lte=24) | Q(sleep_hours__isnull=True),
                name='checkins_sleep_hours_range',
            ),
        ]


class JournalEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='journal_entries')
    content = models.TextField()
    experienced_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'journal_entries'


class TrustedContact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='trusted_contacts')
    display_name = models.CharField(max_length=255)
    relationship = models.CharField(max_length=64)
    contact_type = models.CharField(max_length=32)
    contact_value_encrypted = models.TextField()
    verification_status = models.CharField(max_length=32)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'trusted_contacts'


class SafetyPlan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='safety_plan')
    coping_steps = models.JSONField(default=list)
    personal_notes = models.TextField(blank=True)
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'safety_plans'


class ChatSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_sessions')
    title = models.CharField(max_length=255, blank=True)
    summary = models.TextField(blank=True)
    state_json = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'chat_sessions'


class ChatMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name='messages')
    reply_to_message = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='replies'
    )
    role = models.CharField(max_length=32)
    message_type = models.CharField(max_length=64)
    content = models.TextField()
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'chat_messages'
        indexes = [models.Index(fields=['session', '-created_at'], name='messages_session_time')]


class AnalysisRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='analysis_runs')
    analysis_type = models.CharField(max_length=64)
    trigger_source = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=32)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    sufficiency_status = models.CharField(max_length=32, blank=True)
    missing_fields = models.JSONField(default=list)
    data_quality = models.JSONField(default=dict)
    model_version = models.CharField(max_length=128, blank=True)
    pipeline_version = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'analysis_runs'
        indexes = [models.Index(fields=['user', '-created_at'], name='runs_user_created')]


class AnalysisReport(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis_run = models.OneToOneField(
        AnalysisRun, on_delete=models.CASCADE, related_name='report'
    )
    emotion_summary = models.JSONField(default=dict)
    trend_metrics = models.JSONField(default=dict)
    concern_level = models.CharField(max_length=32)
    summary = models.TextField()
    contributing_factors = models.JSONField(default=list)
    recommendations = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'analysis_reports'


class AnalysisSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis_run = models.ForeignKey(
        AnalysisRun, on_delete=models.CASCADE, related_name='sources'
    )
    check_in = models.ForeignKey(
        CheckIn, null=True, blank=True, on_delete=models.SET_NULL, related_name='analysis_sources'
    )
    journal = models.ForeignKey(
        JournalEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name='analysis_sources'
    )
    chat_message = models.ForeignKey(
        ChatMessage, null=True, blank=True, on_delete=models.SET_NULL, related_name='analysis_sources'
    )

    class Meta:
        db_table = 'analysis_sources'
        constraints = [models.CheckConstraint(
            condition=(
                Q(check_in__isnull=False, journal__isnull=True, chat_message__isnull=True)
                | Q(check_in__isnull=True, journal__isnull=False, chat_message__isnull=True)
                | Q(check_in__isnull=True, journal__isnull=True, chat_message__isnull=False)
            ),
            name='analysis_source_exactly_one',
        )]


class SafetyAlert(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='safety_alerts')
    analysis_run = models.ForeignKey(
        AnalysisRun, null=True, blank=True, on_delete=models.SET_NULL, related_name='safety_alerts'
    )
    trigger_message = models.ForeignKey(
        ChatMessage, null=True, blank=True, on_delete=models.SET_NULL, related_name='safety_alerts'
    )
    reviewer_user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='reviewed_safety_alerts'
    )
    trigger_type = models.CharField(max_length=64, blank=True)
    severity = models.CharField(max_length=32)
    reason = models.TextField()
    status = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'safety_alerts'
        indexes = [models.Index(fields=['reviewer_user', 'status'], name='alerts_reviewer_status')]


class AlertDelivery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    alert = models.ForeignKey(SafetyAlert, on_delete=models.CASCADE, related_name='deliveries')
    trusted_contact = models.ForeignKey(
        TrustedContact, null=True, blank=True, on_delete=models.SET_NULL, related_name='alert_deliveries'
    )
    recipient_user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='received_alert_deliveries'
    )
    channel = models.CharField(max_length=32)
    delivery_status = models.CharField(max_length=32)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = 'alert_deliveries'
        indexes = [models.Index(fields=['alert', 'delivery_status'], name='deliveries_alert_status')]


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    safety_alert = models.ForeignKey(
        SafetyAlert, null=True, blank=True, on_delete=models.SET_NULL, related_name='notifications'
    )
    notification_type = models.CharField(max_length=64)
    title = models.CharField(max_length=255)
    safe_message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'notifications'
