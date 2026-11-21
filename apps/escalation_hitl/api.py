from __future__ import annotations

from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from apps.accounts_consent.services import get_profile_from_request
from apps.escalation_hitl.models import EscalationEpisode
from apps.escalation_hitl.schemas import (
    AuditVerifyResponseDTO,
    BreakGlassInput,
    EpisodeStatusUpdateInput,
    EscalationEpisodeDTO,
    SosDispatchInput,
    SosDispatchResponseDTO,
)
from apps.escalation_hitl.services import (
    activate_break_glass,
    append_safety_audit_log,
    dispatch_warm_sos,
    serialize_episode_dto,
    verify_safety_audit_chain,
)

router = Router(tags=["Escalation HITL, Warm SOS & Audit"])

VALID_EPISODE_STATUSES = {"OPEN", "ACKNOWLEDGED", "INTERVENING", "FOLLOW_UP", "RESOLVED"}


@router.post("/sos", response=SosDispatchResponseDTO)
def trigger_sos(request, payload: SosDispatchInput):
    profile = get_profile_from_request(request)
    return dispatch_warm_sos(
        profile=profile,
        trigger_source=payload.triggerSource or "MANUAL_USER",
        coarse_location=payload.coarseLocation,
    )


@router.get("/episodes", response=list[EscalationEpisodeDTO])
def list_episodes(request):
    # Ensure demo/requester profile exists
    get_profile_from_request(request)
    episodes = EscalationEpisode.objects.select_related("profile").order_by("-opened_at")[:50]
    return [serialize_episode_dto(ep) for ep in episodes]


@router.post("/episodes/{episode_id}/status", response=EscalationEpisodeDTO)
def update_episode_status(request, episode_id: str, payload: EpisodeStatusUpdateInput):
    episode = get_object_or_404(EscalationEpisode, id=episode_id)
    new_status = payload.newStatus.strip().upper()
    if new_status not in VALID_EPISODE_STATUSES:
        raise HttpError(400, f"Invalid episode status '{payload.newStatus}'.")

    old_status = episode.status
    episode.status = new_status
    if payload.counselorNote is not None:
        episode.counselorNote_val = payload.counselorNote
        episode.counselor_note = payload.counselorNote.strip()
    if new_status == "RESOLVED":
        episode.resolved_at = timezone.now()
    episode.save()

    if payload.clearRiskFloor:
        from apps.ai_risk_engine.services import clear_profile_risk_floor

        clear_profile_risk_floor(episode.profile, actor="COUNSELOR_HITL")
        episode.refresh_from_db()

    append_safety_audit_log(
        event_type="EPISODE_STATUS_CHANGED",
        actor="COUNSELOR_HITL",
        episode_id_ref=episode.id,
        summary=(
            f"Cập nhật trạng thái ca {episode.id} từ {old_status} -> {new_status}"
            + (" (đã gỡ khóa sàn rủi ro theo đánh giá lâm sàng)." if payload.clearRiskFloor else ".")
        ),
    )
    return serialize_episode_dto(episode)


@router.post("/episodes/{episode_id}/break-glass", response=EscalationEpisodeDTO)
def break_glass_endpoint(request, episode_id: str, payload: BreakGlassInput):
    episode = get_object_or_404(EscalationEpisode, id=episode_id)
    if not payload.counselorName.strip() or len(payload.clinicalReason.strip()) < 4:
        raise HttpError(400, "Bắt buộc nhập họ tên chuyên gia và lý do lâm sàng hợp lệ để mở khóa Break-Glass.")
    updated = activate_break_glass(
        episode=episode,
        counselor_name=payload.counselorName,
        clinical_reason=payload.clinicalReason,
    )
    return serialize_episode_dto(updated)


@router.get("/audit/verify", response=AuditVerifyResponseDTO)
def verify_audit_endpoint(request):
    return verify_safety_audit_chain(limit_recent=25)
