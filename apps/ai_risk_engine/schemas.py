from __future__ import annotations

from typing import Optional
from ninja import Schema


class ChannelScoresDTO(Schema):
    chat: float = 0.0
    clin: float = 0.0
    mood: float = 0.0
    behav: float = 0.0


class AttributionItemDTO(Schema):
    factor: str
    key: str
    contribution: float
    detail: str


class AdaptiveScreeningPromptDTO(Schema):
    recommendedScale: str  # "PHQ2_GAD2" | "PHQ9" | "GAD7"
    reason: str
    triggeredBy: str


class RiskSnapshotDTO(Schema):
    rInst: float
    rEff: float
    currentTier: str  # "L0" | "L1" | "L2" | "L3" | "L4"
    floorTier: str = "L0"
    floorExpiresAt: Optional[str] = None
    channelScores: ChannelScoresDTO
    attribution: list[AttributionItemDTO]
    paradoxicalCalmDetected: bool = False
    adaptiveScreeningPrompt: Optional[AdaptiveScreeningPromptDTO] = None
    updatedAt: str


class HealthDTO(Schema):
    status: str
    version: str
    geminiConfigured: bool
    model: str
