from __future__ import annotations

from typing import Optional
from ninja import Schema
from apps.ai_risk_engine.schemas import RiskSnapshotDTO


class ScaleQuestionDTO(Schema):
    id: str
    text: str


class ScaleOptionDTO(Schema):
    value: int
    label: str


class ScreeningScaleDTO(Schema):
    scaleType: str
    title: str
    subtitle: str
    questions: list[ScaleQuestionDTO]
    options: list[ScaleOptionDTO]


class ScreeningSubmitInput(Schema):
    scaleType: str
    answers: dict[str, int]


class ScreeningSubmitResponseDTO(Schema):
    scaleType: str
    totalScore: int
    phq9Item9Score: int
    severityLabel: str
    recommendFollowUpScale: Optional[str] = None
    empatheticFeedback: str
    riskSnapshot: RiskSnapshotDTO


class SafetyPlanInput(Schema):
    warningSigns: list[str] = []
    internalCoping: list[str] = []
    safePlacesAndDistractions: list[str] = []
    trustedPeople: list[str] = []
    professionalContacts: list[str] = []
    safeEnvironmentSteps: list[str] = []


class SafetyPlanDTO(Schema):
    warningSigns: list[str]
    internalCoping: list[str]
    safePlacesAndDistractions: list[str]
    trustedPeople: list[str]
    professionalContacts: list[str]
    safeEnvironmentSteps: list[str]
    updatedAt: str
