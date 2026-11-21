"""
URL Configuration for MindCare AI Risk Engine v2.0 (`/api/v1/`).
"""

from django.contrib import admin
from django.urls import path
from ninja import NinjaAPI

from apps.accounts_consent.api import router as consent_contacts_router
from apps.ai_companion.api import router as ai_companion_router
from apps.ai_risk_engine.api import router as risk_engine_router
from apps.community_safety.api import router as community_safety_router
from apps.escalation_hitl.api import router as escalation_hitl_router
from apps.screening.api import router as screening_router
from apps.tracking.api import router as tracking_router

api = NinjaAPI(
    title="MindCare AI Risk Engine API",
    version="2.0.0",
    description="Clinical-Grounded & Privacy-Preserving Hybrid Mental Health Risk Architecture for Vietnamese Youth",
)

# Mount routers to match 100% of API Contract v2.0 paths under /api/v1
api.add_router("", risk_engine_router)          # /health, /risk/state
api.add_router("", consent_contacts_router)     # /consent, /contacts, /contacts/{contact_id}
api.add_router("", tracking_router)             # /checkins, /journals, /journals/{journal_id}, /stats/overview
api.add_router("", ai_companion_router)         # /chat/stream
api.add_router("/screening", screening_router)  # /screening/scales/{scale_type}, /screening/submit, /screening/safety-plan
api.add_router("/community", community_safety_router)   # /community/moderate, /community/letters
api.add_router("/escalation", escalation_hitl_router)   # /escalation/sos, /escalation/episodes, .../status, .../break-glass, .../audit/verify

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
]
