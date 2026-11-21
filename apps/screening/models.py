from __future__ import annotations

import uuid
from django.db import models
from django.utils import timezone
from apps.accounts_consent.models import ClientProfile


class ScreeningSubmission(models.Model):
    SCALE_CHOICES = [
        ("PHQ2_GAD2", "PHQ-2 & GAD-2 Micro-Screen"),
        ("PHQ9", "PHQ-9 Depression Scale"),
        ("GAD7", "GAD-7 Anxiety Scale"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="screenings",
    )
    scale_type = models.CharField(max_length=16, choices=SCALE_CHOICES, db_index=True)
    total_score = models.IntegerField(default=0)
    phq9_item9_score = models.IntegerField(default=0)
    phq2_subscore = models.IntegerField(default=0)
    gad2_subscore = models.IntegerField(default=0)
    answers = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "mc_screening_submissions"
        ordering = ["-created_at"]


class SafetyPlanRecord(models.Model):
    profile = models.OneToOneField(
        ClientProfile,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="safety_plan",
    )
    warning_signs = models.JSONField(default=list)
    internal_coping = models.JSONField(default=list)
    safe_places_and_distractions = models.JSONField(default=list)
    trusted_people = models.JSONField(default=list)
    professional_contacts = models.JSONField(default=list)
    safe_environment_steps = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mc_safety_plans"
