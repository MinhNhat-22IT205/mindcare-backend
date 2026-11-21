from __future__ import annotations

import uuid
from django.db import models
from django.utils import timezone
from apps.accounts_consent.models import ClientProfile


def make_letter_id() -> str:
    return f"letter-{uuid.uuid4().hex[:8]}"


class CommunityPostRecord(models.Model):
    CONTENT_TYPE_CHOICES = [
        ("letter", "Community Letter"),
        ("reply", "Anonymous Reply"),
        ("private_message", "Private Message"),
    ]
    DECISION_CHOICES = [
        ("SAFE_PUBLISH", "SAFE_PUBLISH"),
        ("SENSITIVE_WITH_CW", "SENSITIVE_WITH_CW"),
        ("BLOCK_TOXIC_BULLYING", "BLOCK_TOXIC_BULLYING"),
        ("SELF_HARM_INTERCEPT", "SELF_HARM_INTERCEPT"),
    ]

    id = models.CharField(max_length=48, primary_key=True, default=make_letter_id)
    author = models.ForeignKey(
        ClientProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="community_posts",
    )
    alias = models.CharField(max_length=64, default="Một chiếc lá nhỏ")
    content_type = models.CharField(max_length=24, choices=CONTENT_TYPE_CHOICES, default="letter", db_index=True)
    title = models.CharField(max_length=200, blank=True, default="")
    content = models.TextField()
    topic = models.CharField(max_length=64, default="Cảm xúc")
    decision = models.CharField(max_length=32, choices=DECISION_CHOICES, default="SAFE_PUBLISH", db_index=True)
    moderation_status = models.CharField(max_length=16, default="published", db_index=True)  # "published" | "hidden"
    content_warning = models.CharField(max_length=255, null=True, blank=True)
    reply_count = models.IntegerField(default=0)
    replies_allowed = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "mc_community_posts"
        ordering = ["-created_at"]
