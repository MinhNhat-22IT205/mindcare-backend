from __future__ import annotations

from typing import Optional
from ninja import Schema
from apps.ai_risk_engine.schemas import RiskSnapshotDTO


class CommunityModerateInput(Schema):
    contentType: str = "letter"  # "letter" | "reply" | "private_message"
    title: Optional[str] = ""
    content: str
    topic: Optional[str] = "Cảm xúc"


class CommunityModerateResponseDTO(Schema):
    decision: str  # "SAFE_PUBLISH" | "SENSITIVE_WITH_CW" | "BLOCK_TOXIC_BULLYING" | "SELF_HARM_INTERCEPT"
    moderationStatus: str  # "published" | "hidden"
    contentWarning: Optional[str] = None
    empatheticInterceptMessage: Optional[str] = None
    openSupportPrompt: bool = False
    riskSnapshot: RiskSnapshotDTO


class CommunityLetterDTO(Schema):
    id: str
    alias: str
    title: str = ""
    content: str
    topic: str
    time: str
    date: str
    replyCount: int = 0
    repliesAllowed: bool = True
    status: str = "published"
    decision: str = "SAFE_PUBLISH"
    contentWarning: Optional[str] = None
    isOwn: bool = False
