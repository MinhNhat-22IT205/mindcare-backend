from __future__ import annotations

import uuid
from django.shortcuts import get_object_or_404
from ninja import Router

from apps.accounts_consent.models import TrustedContactRecord, UserConsentProfile
from apps.accounts_consent.schemas import (
    ConsentDTO,
    DeleteResultDTO,
    TrustedContactDTO,
    TrustedContactInput,
    TrustedContactUpdateInput,
)
from apps.accounts_consent.services import get_profile_from_request

router = Router(tags=["Consent & Trusted Contacts"])


def _serialize_contact(rec: TrustedContactRecord) -> dict:
    return {
        "id": rec.external_id,
        "name": rec.name,
        "relationship": rec.relationship,
        "phone": rec.phone,
        "avatarLetter": rec.avatar_letter or (rec.name[:1].upper() if rec.name else "T"),
        "status": rec.status,
        "permissionScope": rec.permission_scope,
        "onlyOnSos": bool(rec.only_on_sos),
        "shareLocation": bool(rec.share_location),
        "allowAutoCrisisSos": bool(rec.allow_auto_crisis_sos),
        # Hard immutable privacy guarantee
        "neverShareJournal": True,
    }


@router.get("/consent", response=ConsentDTO)
def get_consent(request):
    profile = get_profile_from_request(request)
    consent, _ = UserConsentProfile.objects.get_or_create(profile=profile)
    return {
        "allowAiCheckinPersonalize": consent.allow_ai_checkin_personalize,
        "allowAiJournalContext": consent.allow_ai_journal_context,
        "allowAutoCrisisSos": consent.allow_auto_crisis_sos,
        "shareCoarseLocationOnSos": consent.share_coarse_location_on_sos,
        "sosFeatureEnabled": consent.sos_feature_enabled,
    }


@router.put("/consent", response=ConsentDTO)
def update_consent(request, payload: ConsentDTO):
    profile = get_profile_from_request(request)
    consent, _ = UserConsentProfile.objects.get_or_create(profile=profile)
    consent.allow_ai_checkin_personalize = payload.allowAiCheckinPersonalize
    consent.allow_ai_journal_context = payload.allowAiJournalContext
    consent.allow_auto_crisis_sos = payload.allowAutoCrisisSos
    consent.share_coarse_location_on_sos = payload.shareCoarseLocationOnSos
    consent.sos_feature_enabled = payload.sosFeatureEnabled
    consent.save()
    return {
        "allowAiCheckinPersonalize": consent.allow_ai_checkin_personalize,
        "allowAiJournalContext": consent.allow_ai_journal_context,
        "allowAutoCrisisSos": consent.allow_auto_crisis_sos,
        "shareCoarseLocationOnSos": consent.share_coarse_location_on_sos,
        "sosFeatureEnabled": consent.sos_feature_enabled,
    }


@router.get("/contacts", response=list[TrustedContactDTO])
def list_contacts(request):
    profile = get_profile_from_request(request)
    records = TrustedContactRecord.objects.filter(profile=profile).order_by("created_at")
    return [_serialize_contact(r) for r in records]


@router.post("/contacts", response=TrustedContactDTO)
def create_contact(request, payload: TrustedContactInput):
    profile = get_profile_from_request(request)
    ext_id = (payload.id or f"tc-{uuid.uuid4().hex[:8]}").strip()
    avatar = (payload.avatarLetter or (payload.name.strip()[:1].upper() if payload.name.strip() else "T"))[:8]

    rec, created = TrustedContactRecord.objects.update_or_create(
        profile=profile,
        external_id=ext_id,
        defaults={
            "name": payload.name.strip(),
            "relationship": payload.relationship.strip(),
            "phone": payload.phone.strip(),
            "avatar_letter": avatar,
            "status": payload.status or "confirmed",
            "permission_scope": payload.permissionScope or "Chỉ nhận tín hiệu hỗ trợ khi có SOS",
            "only_on_sos": payload.onlyOnSos,
            "share_location": payload.shareLocation,
            "allow_auto_crisis_sos": payload.allowAutoCrisisSos,
            "never_share_journal": True,
        },
    )
    return _serialize_contact(rec)


@router.put("/contacts/{contact_id}", response=TrustedContactDTO)
def update_contact(request, contact_id: str, payload: TrustedContactUpdateInput):
    profile = get_profile_from_request(request)
    rec = get_object_or_404(TrustedContactRecord, profile=profile, external_id=contact_id)

    if payload.name is not None:
        rec.name = payload.name.strip()
    if payload.relationship is not None:
        rec.relationship = payload.relationship.strip()
    if payload.phone is not None:
        rec.phone = payload.phone.strip()
    if payload.avatarLetter is not None:
        rec.avatar_letter = payload.avatarLetter[:8]
    if payload.status is not None:
        rec.status = payload.status
    if payload.permissionScope is not None:
        rec.permission_scope = payload.permissionScope
    if payload.onlyOnSos is not None:
        rec.only_on_sos = payload.onlyOnSos
    if payload.shareLocation is not None:
        rec.share_location = payload.shareLocation
    if payload.allowAutoCrisisSos is not None:
        rec.allow_auto_crisis_sos = payload.allowAutoCrisisSos

    # Enforce hard lock regardless of client input
    rec.never_share_journal = True
    rec.save()
    return _serialize_contact(rec)


@router.delete("/contacts/{contact_id}", response=DeleteResultDTO)
def delete_contact(request, contact_id: str):
    profile = get_profile_from_request(request)
    deleted_count, _ = TrustedContactRecord.objects.filter(
        profile=profile,
        external_id=contact_id,
    ).delete()
    return {"deleted": deleted_count > 0}
