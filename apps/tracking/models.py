from __future__ import annotations

import uuid
from django.db import models
from apps.accounts_consent.models import ClientProfile
from apps.core_security.crypto import decrypt_field, encrypt_field, is_encrypted_token


class CheckInRecord(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="checkins",
    )
    external_id = models.CharField(max_length=64, db_index=True)
    date = models.CharField(max_length=32, db_index=True)  # YYYY-MM-DD
    day_label = models.CharField(max_length=24, default="CN")
    time = models.CharField(max_length=32, default="08:30")
    mood = models.IntegerField(default=6)  # 1..10
    mood_label = models.CharField(max_length=64, default="Ổn định")
    stress = models.IntegerField(default=5)  # 0..10
    stress_label = models.CharField(max_length=64, default="Vừa phải")
    sleep_hours = models.FloatField(default=7.0)
    sleep_quality = models.CharField(max_length=24, default="good")
    emotions = models.JSONField(default=list)
    encrypted_note = models.TextField(blank=True, default="")
    note_negativity = models.FloatField(default=0.1)
    computed_mood_risk = models.FloatField(default=0.12)
    allow_ai_personalize = models.BooleanField(default=True)
    created_at_ms = models.BigIntegerField(default=0, db_index=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mc_checkin_records"
        unique_together = [("profile", "external_id")]
        ordering = ["-date", "-created_at_ms"]

    def set_note(self, plaintext_note: str) -> None:
        aad = f"checkin::{self.profile_id}"
        self.encrypted_note = encrypt_field(plaintext_note or "", aad=aad)

    def get_note(self, requester_profile_id=None) -> str:
        if requester_profile_id is not None and str(requester_profile_id) != str(self.profile_id):
            raise PermissionError("Unauthorized attempt to decrypt another user's check-in note.")
        if not self.encrypted_note:
            return ""
        aad = f"checkin::{self.profile_id}"
        if is_encrypted_token(self.encrypted_note):
            return decrypt_field(self.encrypted_note, aad=aad)
        return self.encrypted_note


class JournalEntryRecord(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="journals",
    )
    external_id = models.CharField(max_length=64, db_index=True)
    date = models.CharField(max_length=64)
    day_label = models.CharField(max_length=32, default="Hôm nay")
    time = models.CharField(max_length=32, default="21:00")
    title = models.CharField(max_length=255, default="Nhật ký cá nhân")
    encrypted_content = models.TextField()  # Stored ONLY as AES-256-GCM ciphertext
    tag = models.CharField(max_length=64, default="Nhẹ nhõm")
    mood_score = models.IntegerField(default=6)
    ai_estimate_label = models.CharField(max_length=64, default="Ổn định nhẹ nhàng")
    ai_estimate_score = models.IntegerField(default=6)
    ai_bar_width = models.IntegerField(default=60)
    negativity_index = models.FloatField(default=0.1)
    is_encrypted = models.BooleanField(default=True)
    allow_ai_ref = models.BooleanField(default=False)
    created_at_ms = models.BigIntegerField(default=0, db_index=True)
    image_url = models.CharField(max_length=512, null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mc_journal_entries"
        unique_together = [("profile", "external_id")]
        ordering = ["-created_at_ms", "-recorded_at"]

    def set_plaintext_content(self, plaintext: str) -> None:
        aad = f"journal::{self.profile_id}"
        self.encrypted_content = encrypt_field(plaintext or "", aad=aad)
        self.is_encrypted = True

    def get_decrypted_content(self, requester_profile_id=None) -> str:
        if requester_profile_id is not None and str(requester_profile_id) != str(self.profile_id):
            raise PermissionError("Unauthorized attempt to decrypt another user's private journal.")
        aad = f"journal::{self.profile_id}"
        return decrypt_field(self.encrypted_content, aad=aad)
