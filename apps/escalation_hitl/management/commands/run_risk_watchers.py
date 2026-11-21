"""
Management command: `python manage.py run_risk_watchers`
Scans longitudinal behavioral anomalies (Digital Phenotyping):
1. Cliffhanger Drop-off (high-risk chat x_chat >= 0.55 followed by >= 10 minutes of sudden silence)
2. Silent Dropout MNAR (elevated baseline R_eff >= 0.45 with >= 48 hours of complete inactivity)
"""

from __future__ import annotations

from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.ai_risk_engine.models import RiskStateSnapshot
from apps.ai_risk_engine.services import record_behavioral_signal
from apps.escalation_hitl.services import append_safety_audit_log, dispatch_warm_sos


class Command(BaseCommand):
    help = "Scan RiskStateSnapshots for Cliffhanger Drop-off (10m) and Silent Dropout MNAR (48h)."

    def handle(self, *args, **options):
        now = timezone.now()
        cliffhanger_cutoff = now - timedelta(minutes=10)
        dropout_cutoff = now - timedelta(hours=48)

        cliffhanger_count = 0
        dropout_count = 0

        # 1. Scan Cliffhanger Drop-offs
        cliff_qs = RiskStateSnapshot.objects.select_related("profile").filter(
            last_high_chat_score__gte=0.55,
            last_user_chat_at__lte=cliffhanger_cutoff,
            cliffhanger_flagged=False,
        )
        for snap in cliff_qs:
            snap.cliffhanger_flagged = True
            snap.save(update_fields=["cliffhanger_flagged", "updated_at"])
            updated_dto = record_behavioral_signal(
                profile=snap.profile,
                behav_score=0.62,
                detail="Cliffhanger Drop-off: ngừng phản hồi đột ngột >10 phút sau tin nhắn rủi ro cao",
            )
            append_safety_audit_log(
                event_type="WATCHER_CLIFFHANGER_DETECTED",
                actor="WATCHER_CRON",
                summary=(
                    f"Phát hiện Cliffhanger Drop-off (>10 phút im lặng sau chat nguy cơ cao) ở hồ sơ "
                    f"{snap.profile.pseudonym_id}; R_eff={updated_dto['rEff']:.2f} ({updated_dto['currentTier']})."
                ),
            )
            if updated_dto["currentTier"] == "L4":
                dispatch_warm_sos(profile=snap.profile, trigger_source="CLIFFHANGER_WATCHER")
            cliffhanger_count += 1

        # 2. Scan Silent Dropout (MNAR >= 48h inactivity while baseline R_eff >= 0.45)
        dropout_qs = RiskStateSnapshot.objects.select_related("profile").filter(
            r_eff__gte=0.45,
            last_activity_at__lte=dropout_cutoff,
            silent_dropout_flagged=False,
        )
        for snap in dropout_qs:
            snap.silent_dropout_flagged = True
            snap.adaptive_screening_prompt = {
                "recommendedScale": "PHQ2_GAD2",
                "reason": "Mấy ngày nay không thấy bạn ghé khu vườn nhỏ, Mochi gửi một lời hỏi thăm thật nhẹ tới bạn nhé. Nếu tiện, bạn chỉ cần chạm nhẹ 4 câu rà soát ngắn này thôi.",
                "triggeredBy": "SILENT_DROPOUT_MNAR",
            }
            snap.save(update_fields=["silent_dropout_flagged", "adaptive_screening_prompt", "updated_at"])
            updated_dto = record_behavioral_signal(
                profile=snap.profile,
                behav_score=0.54,
                detail="Silent Dropout (MNAR): gián đoạn tương tác >48h khi đang có nền rủi ro trung bình-cao",
            )
            append_safety_audit_log(
                event_type="WATCHER_SILENT_DROPOUT_DETECTED",
                actor="WATCHER_CRON",
                summary=(
                    f"Phát hiện Silent Dropout (>48h ngừng mở app trên nền rủi ro cao) ở hồ sơ "
                    f"{snap.profile.pseudonym_id}; R_eff={updated_dto['rEff']:.2f}."
                ),
            )
            dropout_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Risk Watchers completed: cliffhangers={cliffhanger_count}, silent_dropouts={dropout_count}"
            )
        )
