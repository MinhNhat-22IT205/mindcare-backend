from __future__ import annotations

import hashlib
import uuid
from django.db import models

DEFAULT_CLIENT_ID = "stu-demo-minh-nguyen"


def make_pseudonym_id(client_id: str) -> str:
    digest = hashlib.sha256(f"mindcare-pseudo::{client_id}".encode("utf-8")).hexdigest()[:4].upper()
    return f"STU-{digest}"


class ClientProfile(models.Model):
    ROLE_CHOICES = [
        ("student", "Student"),
        ("counselor", "Counselor"),
        ("safety_admin", "Safety Admin"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client_id = models.CharField(max_length=128, unique=True, db_index=True)
    pseudonym_id = models.CharField(max_length=32, db_index=True)
    display_alias = models.CharField(max_length=128, default="Sinh viên ẩn danh")
    role = models.CharField(max_length=32, choices=ROLE_CHOICES, default="student")
    seeded_demo_data = models.BooleanField(default=False)
    last_seen_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mc_client_profiles"

    def save(self, *args, **kwargs):
        if not self.pseudonym_id:
            self.pseudonym_id = make_pseudonym_id(self.client_id)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.client_id} ({self.pseudonym_id})"


class UserConsentProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.OneToOneField(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="consent",
    )
    allow_ai_checkin_personalize = models.BooleanField(default=True)
    allow_ai_journal_context = models.BooleanField(default=False)
    allow_auto_crisis_sos = models.BooleanField(default=False)
    share_coarse_location_on_sos = models.BooleanField(default=False)
    sos_feature_enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mc_user_consent_profiles"


class TrustedContactRecord(models.Model):
    STATUS_CHOICES = [
        ("confirmed", "Confirmed"),
        ("pending", "Pending"),
    ]

    uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="trusted_contacts",
    )
    external_id = models.CharField(max_length=64, db_index=True)
    name = models.CharField(max_length=128)
    relationship = models.CharField(max_length=64)
    phone = models.CharField(max_length=32)
    avatar_letter = models.CharField(max_length=8, default="T")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="confirmed")
    permission_scope = models.CharField(
        max_length=255,
        default="Chỉ nhận tín hiệu hỗ trợ khi có SOS",
    )
    only_on_sos = models.BooleanField(default=True)
    share_location = models.BooleanField(default=False)
    allow_auto_crisis_sos = models.BooleanField(default=False)
    # Hard privacy invariant: NEVER share raw journal or chat content with relatives/friends
    never_share_journal = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mc_trusted_contacts"
        unique_together = [("profile", "external_id")]
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        # Enforce immutable privacy lock regardless of caller payload
        self.never_share_journal = True
        if not self.avatar_letter and self.name:
            stripped = self.name.strip()
            self.avatar_letter = stripped[0].upper() if stripped else "T"
        super().save(*args, **kwargs)
