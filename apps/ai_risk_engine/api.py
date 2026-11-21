from __future__ import annotations

from django.conf import settings
from ninja import Router

from apps.accounts_consent.services import get_profile_from_request
from apps.ai_risk_engine.schemas import HealthDTO, RiskSnapshotDTO
from apps.ai_risk_engine.services import get_or_create_risk_snapshot, serialize_risk_snapshot

router = Router(tags=["Health & Risk State"])


@router.get("/health", response=HealthDTO)
def health_check(request):
    gemini_key = getattr(settings, "GEMINI_API_KEY", "").strip()
    return {
        "status": "ok",
        "version": getattr(settings, "MINDCARE_VERSION", "2.0.0"),
        "geminiConfigured": bool(gemini_key),
        "model": getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash"),
    }


@router.get("/risk/state", response=RiskSnapshotDTO)
def get_risk_state(request):
    profile = get_profile_from_request(request)
    snap = get_or_create_risk_snapshot(profile)
    return serialize_risk_snapshot(snap, refresh_decay=True)
