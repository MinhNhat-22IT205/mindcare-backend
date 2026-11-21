from __future__ import annotations

import hashlib
import json
import uuid
from django.db import models
from django.utils import timezone
from apps.accounts_consent.models import ClientProfile
from apps.core_security.crypto import decrypt_field, encrypt_field, is_encrypted_token

GENESIS_PREV_HASH = "0" * 64


def generate_episode_id() -> str:
    return f"ep-{uuid.uuid4().hex[:10]}"


class EscalationEpisode(models.Model):
    PRIORITY_CHOICES = [
        ("P0", "P0 - Critical (<5 min SLA)"),
        ("P1", "P1 - High (<30 min SLA)"),
        ("P2", "P2 - Moderate Review (<24h SLA)"),
    ]
    STATUS_CHOICES = [
        ("OPEN", "OPEN"),
        ("ACKNOWLEDGED", "ACKNOWLEDGED"),
        ("INTERVENING", "INTERVENING"),
        ("FOLLOW_UP", "FOLLOW_UP"),
        ("RESOLVED", "RESOLVED"),
    ]

    id = models.CharField(max_length=48, primary_key=True, default=generate_episode_id)
    profile = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="escalation_episodes",
    )
    pseudonym_id = models.CharField(max_length=32, db_index=True)
    priority = models.CharField(max_length=8, choices=PRIORITY_CHOICES, default="P1", db_index=True)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default="OPEN", db_index=True)
    peak_risk_score = models.FloatField(default=0.0)
    current_tier = models.CharField(max_length=8, default="L2")

    sbar = models.JSONField(default=dict)
    encrypted_evidence_quotes = models.TextField(blank=True, default="")
    attribution = models.JSONField(default=list)

    break_glass_active = models.BooleanField(default=False)
    break_glass_expires_at = models.DateTimeField(null=True, blank=True)
    break_glass_counselor = models.CharField(max_length=128, blank=True, default="")
    break_glass_reason = models.TextField(blank=True, default="")

    coarse_location = models.CharField(max_length=128, blank=True, default="Khu đô thị ĐH Đà Nẵng (ước tính cấp Quận)")
    counselor_note = models.TextField(blank=True, default="")

    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mc_escalation_episodes"
        ordering = ["-opened_at"]

    def set_evidence_quotes(self, quotes: list[str]) -> None:
        clean = [q.strip()[:280] for q in quotes if q and q.strip()][:6]
        aad = f"episode-quotes::{self.id}"
        self.encrypted_evidence_quotes = encrypt_field(json.dumps(clean, ensure_ascii=False), aad=aad)

    def get_evidence_quotes(self) -> list[str]:
        if not self.encrypted_evidence_quotes:
            return []
        aad = f"episode-quotes::{self.id}"
        try:
            if is_encrypted_token(self.encrypted_evidence_quotes):
                raw = decrypt_field(self.encrypted_evidence_quotes, aad=aad)
                return list(json.loads(raw))
        except Exception:
            return []
        return []


class NotificationDispatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="sos_dispatches",
    )
    episode = models.ForeignKey(
        EscalationEpisode,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatches",
    )
    trigger_source = models.CharField(max_length=32, default="MANUAL_USER")
    recipients_count = models.IntegerField(default=0)
    never_shared_journal_verified = models.BooleanField(default=True)
    warm_message_preview = models.TextField()
    coarse_location = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "mc_notification_dispatches"
        ordering = ["-created_at"]


class SafetyAuditLog(models.Model):
    """
    Append-Only Immutable Audit Log chained via SHA-256 cryptographic hashes.
    """

    id = models.BigAutoField(primary_key=True)
    event_type = models.CharField(max_length=64, db_index=True)
    actor = models.CharField(max_length=128)
    episode_id_ref = models.CharField(max_length=64, blank=True, default="")
    summary = models.TextField()
    timestamp_iso = models.CharField(max_length=64)
    prev_hash = models.CharField(max_length=64, default=GENESIS_PREV_HASH)
    entry_hash = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "mc_safety_audit_logs"
        ordering = ["id"]

    @staticmethod
    def compute_sha256(
        prev_hash: str,
        event_type: str,
        actor: str,
        timestamp_iso: str,
        summary: str,
    ) -> str:
        canonical = f"{prev_hash}|{event_type}|{actor}|{timestamp_iso}|{summary}"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
