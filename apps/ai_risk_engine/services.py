"""
High-level Risk State Orchestration Service (`apps/ai_risk_engine/services.py`).

Connects all 4 channels (`chat`, `clin`, `mood`, `behav`), persists `RiskStateSnapshot` + `RiskSignalEvent`,
evaluates Two-Stage Adaptive Screening triggers, and synchronizes with `escalation_hitl`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from django.conf import settings
from django.utils import timezone

from apps.accounts_consent.models import ClientProfile
from apps.ai_risk_engine.fusion_math import (
    CHANNELS,
    compute_clinical_channel_score,
    compute_fusion_step,
    compute_mood_channel_score,
)
from apps.ai_risk_engine.models import RiskSignalEvent, RiskStateSnapshot
from apps.escalation_hitl.services import dispatch_warm_sos, upsert_escalation_episode
from apps.screening.models import ScreeningSubmission


def get_or_create_risk_snapshot(profile: ClientProfile) -> RiskStateSnapshot:
    snap, created = RiskStateSnapshot.objects.get_or_create(
        profile=profile,
        defaults={
            "r_inst": 0.0,
            "r_eff": 0.0,
            "current_tier": "L0",
            "floor_tier": "L0",
            "floor_score": 0.0,
            "channel_raw_scores": {"chat": 0.0, "clin": 0.0, "mood": 0.0, "behav": 0.0},
            "channel_timestamps": {},
            "channel_scores": {"chat": 0.0, "clin": 0.0, "mood": 0.0, "behav": 0.0},
            "channel_details": {"mood": "Tâm trạng ổn định"},
            "attribution": [
                {
                    "factor": "Check-in & Nhật ký",
                    "key": "mood",
                    "contribution": 0.0,
                    "detail": "Tâm trạng ổn định",
                }
            ],
        },
    )
    return snap


def serialize_risk_snapshot(snap: RiskStateSnapshot, refresh_decay: bool = False) -> dict:
    if refresh_decay and snap.channel_timestamps:
        step = compute_fusion_step(
            raw_scores=dict(snap.channel_raw_scores or {}),
            raw_timestamps=dict(snap.channel_timestamps or {}),
            channel_details=dict(snap.channel_details or {}),
            prev_r_eff=snap.r_eff,
            prev_tier=snap.current_tier,
            prev_floor_tier=snap.floor_tier,
            prev_floor_score=snap.floor_score,
            prev_floor_expires_at=snap.floor_expires_at,
        )
        snap.r_inst = step.r_inst
        snap.r_eff = step.r_eff
        snap.current_tier = step.current_tier
        snap.floor_tier = step.floor_tier
        snap.floor_score = step.floor_score
        snap.floor_expires_at = step.floor_expires_at
        snap.channel_scores = step.decayed_scores
        snap.attribution = step.attribution
        snap.save()

    scores = dict(snap.channel_scores or {})
    floor_exp_str = (
        timezone.localtime(snap.floor_expires_at).isoformat()
        if (snap.floor_expires_at and snap.floor_expires_at > timezone.now() and snap.floor_tier != "L0")
        else None
    )
    eff_floor_tier = snap.floor_tier if floor_exp_str else "L0"

    return {
        "rInst": round(float(snap.r_inst), 4),
        "rEff": round(float(snap.r_eff), 4),
        "currentTier": snap.current_tier,
        "floorTier": eff_floor_tier,
        "floorExpiresAt": floor_exp_str,
        "channelScores": {
            "chat": round(float(scores.get("chat", 0.0)), 4),
            "clin": round(float(scores.get("clin", 0.0)), 4),
            "mood": round(float(scores.get("mood", 0.0)), 4),
            "behav": round(float(scores.get("behav", 0.0)), 4),
        },
        "attribution": list(snap.attribution or []),
        "paradoxicalCalmDetected": bool(snap.paradoxical_calm_detected),
        "adaptiveScreeningPrompt": snap.adaptive_screening_prompt,
        "updatedAt": timezone.localtime(snap.updated_at).isoformat(),
    }


def _evaluate_adaptive_screening_prompt(
    profile: ClientProfile,
    current_tier: str,
    decayed_scores: dict[str, float],
) -> dict | None:
    """
    Two-Stage Adaptive Micro-Screening Trigger:
    - Never interrupt an acute L4 crisis with a questionnaire.
    - Stage 2 upgrade: If recent PHQ2_GAD2 had PHQ-2 >= 3 -> recommend PHQ9; if GAD-2 >= 3 -> recommend GAD7.
    - Stage 1 micro-screen: If user reaches L2/L3 or mood >= 0.45 without recent screening -> recommend PHQ2_GAD2.
    """
    if current_tier == "L4":
        return None

    now = timezone.now()
    recent_full = ScreeningSubmission.objects.filter(
        profile=profile,
        scale_type__in=["PHQ9", "GAD7"],
        created_at__gte=now - timedelta(days=7),
    ).first()
    if recent_full:
        return None

    recent_micro = ScreeningSubmission.objects.filter(
        profile=profile,
        scale_type="PHQ2_GAD2",
        created_at__gte=now - timedelta(days=7),
    ).first()

    if recent_micro:
        if recent_micro.phq2_subscore >= 3:
            return {
                "recommendedScale": "PHQ9",
                "reason": "Từ 4 câu rà soát ngắn vừa rồi, Mochi thấy năng lượng và hứng thú của bạn đang giảm rõ. Bạn làm thêm vài câu PHQ-9 ngắn để mình hiểu đúng mức độ nhé?",
                "triggeredBy": "PHQ2_POSITIVE_FOLLOWUP",
            }
        if recent_micro.gad2_subscore >= 3:
            return {
                "recommendedScale": "GAD7",
                "reason": "Mochi nhận thấy mức lo âu của bạn đang khá căng. Bạn có muốn làm tiếp thang ngắn GAD-7 để mình gợi ý bài tập hạ nhiệt phù hợp nhất không?",
                "triggeredBy": "GAD2_POSITIVE_FOLLOWUP",
            }
        return None

    if current_tier in ("L2", "L3") or decayed_scores.get("mood", 0.0) >= 0.45:
        return {
            "recommendedScale": "PHQ2_GAD2",
            "reason": "Mochi nhận thấy vài ngày gần đây năng lượng và giấc ngủ của bạn hơi giảm. Bạn có muốn làm nhanh 4 câu rà soát ngắn (khoảng 30 giây) để mình hỗ trợ sát hơn không?",
            "triggeredBy": "MODERATE_DRIFT",
        }

    return None


def _apply_channel_update(
    profile: ClientProfile,
    updates: dict[str, tuple[float, str]],
    source_label: str,
    cssrs_stage: int = 0,
    pragmatic_intent: str = "NONE",
    evidence_text: str = "",
    paradoxical_calm: bool | None = None,
    clear_risk_floor: bool = False,
    event_time: datetime | None = None,
) -> dict:
    ref_now = event_time or timezone.now()
    snap = get_or_create_risk_snapshot(profile)

    raw_scores = {ch: float((snap.channel_raw_scores or {}).get(ch, 0.0)) for ch in CHANNELS}
    raw_ts = dict(snap.channel_timestamps or {})
    details = dict(snap.channel_details or {})

    for ch, (new_val, detail_str) in updates.items():
        raw_scores[ch] = float(max(0.0, min(1.0, new_val)))
        raw_ts[ch] = ref_now.isoformat()
        if detail_str:
            details[ch] = detail_str

    step = compute_fusion_step(
        raw_scores=raw_scores,
        raw_timestamps=raw_ts,
        channel_details=details,
        prev_r_eff=snap.r_eff,
        prev_tier=snap.current_tier,
        prev_floor_tier=snap.floor_tier,
        prev_floor_score=snap.floor_score,
        prev_floor_expires_at=snap.floor_expires_at,
        clear_risk_floor=clear_risk_floor,
        now=ref_now,
    )

    snap.channel_raw_scores = raw_scores
    snap.channel_timestamps = raw_ts
    snap.channel_details = details
    snap.channel_scores = step.decayed_scores
    snap.r_inst = step.r_inst
    snap.r_eff = step.r_eff
    snap.current_tier = step.current_tier
    snap.floor_tier = step.floor_tier
    snap.floor_score = step.floor_score
    snap.floor_expires_at = step.floor_expires_at
    snap.attribution = step.attribution

    if paradoxical_calm is not None:
        snap.paradoxical_calm_detected = paradoxical_calm

    if "chat" in updates:
        chat_val = updates["chat"][0]
        snap.last_user_chat_at = ref_now
        snap.last_high_chat_score = chat_val
        snap.cliffhanger_flagged = False

    if source_label != "WATCHER":
        snap.last_activity_at = ref_now
        snap.silent_dropout_flagged = False
        snap.adaptive_screening_prompt = _evaluate_adaptive_screening_prompt(
            profile=profile,
            current_tier=step.current_tier,
            decayed_scores=step.decayed_scores,
        )
    elif not snap.adaptive_screening_prompt:
        snap.adaptive_screening_prompt = _evaluate_adaptive_screening_prompt(
            profile=profile,
            current_tier=step.current_tier,
            decayed_scores=step.decayed_scores,
        )
    snap.save()

    # Log signal event with AES-256-GCM encrypted evidence
    primary_raw = max((v[0] for v in updates.values()), default=0.0)
    ev = RiskSignalEvent(
        profile=profile,
        source_channel=source_label if source_label in dict(RiskSignalEvent.SOURCE_CHOICES) else "BEHAV",
        raw_score=primary_raw,
        cssrs_stage=cssrs_stage,
        pragmatic_intent=pragmatic_intent,
        attribution_factors=step.attribution,
        created_at=ref_now,
    )
    if evidence_text:
        ev.set_evidence(evidence_text)
    ev.save()

    # Synchronize Escalation Episode (with Episode Deduplication)
    upsert_escalation_episode(
        profile=profile,
        tier=step.current_tier,
        r_eff=step.r_eff,
        attribution=step.attribution,
        source_channel=source_label,
        evidence_quote=evidence_text if step.current_tier in ("L3", "L4") else None,
    )

    return serialize_risk_snapshot(snap)


def record_chat_signal(
    profile: ClientProfile,
    x_chat_score: float,
    cssrs_stage: int,
    pragmatic_intent: str,
    evidence_text: str = "",
    nocturnal_detected: bool = False,
    nocturnal_hhmm: str = "",
    event_time: datetime | None = None,
) -> dict:
    if cssrs_stage >= 4:
        detail = f"C-SSRS Stage {cssrs_stage} (Ý định/Kế hoạch cấp tính)"
    elif cssrs_stage >= 2:
        detail = f"C-SSRS Stage {cssrs_stage} (Ý nghĩ tự hại chủ động)"
    elif cssrs_stage == 1:
        detail = "C-SSRS Stage 1 (Ý nghĩ buông xuôi thụ động / Xác minh an toàn)"
    else:
        detail = "Trò chuyện bình thường (C-SSRS Stage 0)"

    updates: dict[str, tuple[float, str]] = {"chat": (x_chat_score, detail)}
    if nocturnal_detected:
        b_score = 0.58 if x_chat_score >= 0.42 else 0.38
        updates["behav"] = (b_score, f"Tương tác đêm khuya ({nocturnal_hhmm})")

    return _apply_channel_update(
        profile=profile,
        updates=updates,
        source_label="CHAT",
        cssrs_stage=cssrs_stage,
        pragmatic_intent=pragmatic_intent,
        evidence_text=evidence_text if cssrs_stage >= 2 else "",
        event_time=event_time,
    )


def record_mood_tracking_signal(
    profile: ClientProfile,
    mood_1_to_10: int,
    stress_0_to_10: int,
    sleep_hours: float,
    journal_negativity: float,
    prior_mood_risks: list[float] | None = None,
    closure_cue_detected: bool = False,
    nocturnal_detected: bool = False,
    nocturnal_hhmm: str = "",
    source_label: str = "CHECKIN",
    event_time: datetime | None = None,
) -> tuple[dict, float]:
    x_mood, paradoxical, detail = compute_mood_channel_score(
        mood_1_to_10=mood_1_to_10,
        stress_0_to_10=stress_0_to_10,
        sleep_hours=sleep_hours,
        journal_negativity=journal_negativity,
        prior_mood_risks=prior_mood_risks,
        closure_cue_detected=closure_cue_detected,
    )
    updates: dict[str, tuple[float, str]] = {"mood": (x_mood, detail)}
    if nocturnal_detected:
        b_score = 0.60 if x_mood >= 0.45 else 0.40
        updates["behav"] = (b_score, f"Ghi nhận lúc đêm khuya ({nocturnal_hhmm})")

    dto = _apply_channel_update(
        profile=profile,
        updates=updates,
        source_label=source_label,
        paradoxical_calm=paradoxical,
        event_time=event_time,
    )
    return dto, x_mood


def record_screening_signal(
    profile: ClientProfile,
    phq9_total: int | None = None,
    gad7_total: int | None = None,
    phq9_item9: int = 0,
    phq2_total: int | None = None,
    gad2_total: int | None = None,
    event_time: datetime | None = None,
) -> dict:
    x_clin, detail = compute_clinical_channel_score(
        phq9_total=phq9_total,
        gad7_total=gad7_total,
        phq9_item9=phq9_item9,
        phq2_total=phq2_total,
        gad2_total=gad2_total,
    )
    evidence = f"Sàng lọc lâm sàng: {detail}" if (x_clin >= 0.68 or phq9_item9 >= 1) else ""
    return _apply_channel_update(
        profile=profile,
        updates={"clin": (x_clin, detail)},
        source_label="SCREENING",
        evidence_text=evidence,
        event_time=event_time,
    )


def record_behavioral_signal(
    profile: ClientProfile,
    behav_score: float,
    detail: str,
    event_time: datetime | None = None,
) -> dict:
    return _apply_channel_update(
        profile=profile,
        updates={"behav": (behav_score, detail)},
        source_label="WATCHER",
        event_time=event_time,
    )


def record_community_intercept_signal(
    profile: ClientProfile,
    intercept_score: float,
    cssrs_stage: int,
    evidence_text: str,
) -> dict:
    detail = f"Chặn tự hại tại Góc sẻ chia (C-SSRS Stage {max(2, cssrs_stage)})"
    return _apply_channel_update(
        profile=profile,
        updates={"chat": (max(0.68, intercept_score), detail)},
        source_label="LETTER",
        cssrs_stage=max(2, cssrs_stage),
        pragmatic_intent="GENUINE_DISTRESS",
        evidence_text=evidence_text,
    )


def clear_profile_risk_floor(profile: ClientProfile, actor: str = "COUNSELOR_HITL") -> dict:
    snap = get_or_create_risk_snapshot(profile)
    raw_scores = dict(snap.channel_raw_scores or {})
    # Reset acute chat peak when clinician confirms safe step-down clearance
    if raw_scores.get("chat", 0.0) > 0.30:
        raw_scores["chat"] = 0.20
    snap.channel_raw_scores = raw_scores
    snap.save(update_fields=["channel_raw_scores", "updated_at"])

    return _apply_channel_update(
        profile=profile,
        updates={},
        source_label="BEHAV",
        clear_risk_floor=True,
    )
