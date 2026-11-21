from __future__ import annotations

from typing import Optional
from ninja import Schema
from apps.ai_risk_engine.schemas import AttributionItemDTO


class SosDispatchInput(Schema):
    triggerSource: str = "MANUAL_USER"
    coarseLocation: Optional[str] = None


class CommunicationGuideDTO(Schema):
    dos: list[str]
    donts: list[str]


class SosDispatchResponseDTO(Schema):
    dispatched: bool
    recipientsCount: int
    neverSharedJournalVerified: bool = True
    neverShareJournalVerified: bool = True
    warmMessagePreview: str
    communicationGuide: CommunicationGuideDTO
    timestamp: str


class SbarBriefDTO(Schema):
    situation: str
    background: str
    assessment: str
    recommendation: str
    evidenceQuotes: list[str] = []


class UnlockedContactInfoDTO(Schema):
    name: str
    relationship: str
    phone: str


class UnlockedEmergencyInfoDTO(Schema):
    studentAlias: str
    trustedContacts: list[UnlockedContactInfoDTO]
    coarseLocation: str


class EscalationEpisodeDTO(Schema):
    id: str
    pseudonymId: str
    priority: str
    status: str
    peakRiskScore: float
    currentTier: str
    sbar: SbarBriefDTO
    attribution: list[AttributionItemDTO]
    breakGlassActive: bool
    breakGlassExpiresAt: Optional[str] = None
    unlockedEmergencyInfo: Optional[UnlockedEmergencyInfoDTO] = None
    counselorNote: str = ""
    openedAt: str
    updatedAt: str


class EpisodeStatusUpdateInput(Schema):
    newStatus: str
    counselorNote: Optional[str] = ""
    clearRiskFloor: bool = False


class BreakGlassInput(Schema):
    counselorName: str
    clinicalReason: str


class AuditLogEntryDTO(Schema):
    id: int
    eventType: str
    actor: str
    summary: str
    timestamp: str
    prevHash: str
    entryHash: str


class AuditVerifyResponseDTO(Schema):
    valid: bool
    totalEntries: int
    latestHash: str
    verifiedAt: str
    recentLogs: list[AuditLogEntryDTO]
