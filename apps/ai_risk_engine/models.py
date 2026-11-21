from __future__ import annotations

import uuid
from django.db import models
from django.utils import timezone
from apps.accounts_consent.models import ClientProfile
from apps.core_security.crypto import decrypt_field, encrypt_field, is_encrypted_token


class RiskStateSnapshot(models.Model):
    profile = models.OneToOneField(
        ClientProfile,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="risk_snapshot",
    )
    r_inst = models.FloatField(default=0.0)
    r_eff = models.FloatField(default=0.0)
    current_tier = models.CharField(max_length=8, default="L0", db_index=True)
    floor_tier = models.CharField(max_length=8, default="L0")
    floor_score = models.FloatField(default=0.0)
    floor_expires_at = models.DateTimeField(null=True, blank=True)

    # Raw channel scores x_m(t_m), timestamps t_m, decayed s_tilde_m(t), and Vietnamese XAI detail strings
    channel_raw_scores = models.JSONField(default=dict)
    channel_timestamps = models.JSONField(default=dict)
    channel_scores = models.JSONField(default=dict)
    channel_details = models.JSONField(default=dict)
    attribution = models.JSONField(default=list)

    paradoxical_calm_detected = models.BooleanField(default=False)
    adaptive_screening_prompt = models.JSONField(null=True, blank=True)

    # Behavioral & cliffhanger/dropout tracking
    last_user_chat_at = models.DateTimeField(null=True, blank=True)
    last_high_chat_score = models.FloatField(default=0.0)
    cliffhanger_flagged = models.BooleanField(default=False)
    silent_dropout_flagged = models.BooleanField(default=False)
    last_activity_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mc_risk_state_snapshots"


class RiskSignalEvent(models.Model):
    SOURCE_CHOICES = [
        ("CHAT", "Chat"),
        ("JOURNAL", "Journal"),
        ("CHECKIN", "Check-in"),
        ("SCREENING", "Screening"),
        ("BEHAV", "Behavioral"),
        ("LETTER", "Community Letter/Reply"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="risk_events",
    )
    source_channel = models.CharField(max_length=16, choices=SOURCE_CHOICES, db_index=True)
    raw_score = models.FloatField(default=0.0)
    cssrs_stage = models.IntegerField(default=0)
    pragmatic_intent = models.CharField(max_length=48, default="NONE")
    encrypted_evidence = models.TextField(blank=True, default="")
    attribution_factors = models.JSONField(default=list)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "mc_risk_signal_events"
        ordering = ["-created_at"]

    def set_evidence(self, plaintext: str) -> None:
        if not plaintext:
            self.encrypted_evidence = ""
            return
        aad = f"evidence::{self.profile_id}"
        self.encrypted_evidence = encrypt_field(plaintext[:500], aad=aad)

    def get_evidence(self) -> str:
        if not self.encrypted_evidence:
            return ""
        aad = f"evidence::{self.profile_id}"
        if is_encrypted_token(self.encrypted_evidence):
            return decrypt_field(self.encrypted_evidence, aad=aad)
        return self.encrypted_evidence
