from __future__ import annotations

from typing import Optional
from ninja import Schema
from apps.ai_risk_engine.schemas import RiskSnapshotDTO


class CheckInDTO(Schema):
    id: str
    date: str
    dayLabel: str
    time: str
    mood: int
    moodLabel: str
    stress: int
    stressLabel: str
    sleepHours: float
    sleepQuality: str = "good"
    emotions: list[str] = []
    note: str = ""
    allowAiPersonalize: bool = True
    createdAt: int


class CheckInInput(Schema):
    id: Optional[str] = None
    date: str
    dayLabel: str = "CN"
    time: str = "08:30"
    mood: int
    moodLabel: str = "Ổn định"
    stress: int
    stressLabel: str = "Vừa phải"
    sleepHours: float = 7.0
    sleepQuality: str = "good"
    emotions: list[str] = []
    note: str = ""
    allowAiPersonalize: bool = True
    createdAt: Optional[int] = None


class CheckInSaveResponseDTO(Schema):
    checkIn: CheckInDTO
    riskSnapshot: RiskSnapshotDTO


class JournalEntryDTO(Schema):
    id: str
    date: str
    dayLabel: str
    time: str
    title: str
    content: str
    tag: str
    moodScore: int
    aiEstimateLabel: str
    aiEstimateScore: int
    aiBarWidth: int
    isEncrypted: bool = True
    allowAiRef: bool = False
    createdAt: int
    imageUrl: Optional[str] = None


class JournalEntryInput(Schema):
    id: Optional[str] = None
    date: str
    dayLabel: str = "Hôm nay"
    time: str = "21:00"
    title: str = "Nhật ký cá nhân"
    content: str
    tag: str = "Nhẹ nhõm"
    moodScore: int = 6
    allowAiRef: bool = False
    createdAt: Optional[int] = None
    imageUrl: Optional[str] = None


class JournalSaveResponseDTO(Schema):
    journal: JournalEntryDTO
    riskSnapshot: RiskSnapshotDTO


class DailySeriesPointDTO(Schema):
    date: str
    dayLabel: str
    mood: float
    stress: float
    sleepHours: float


class AiStatInsightDTO(Schema):
    title: str
    body: str
    tone: str


class StatsOverviewDTO(Schema):
    avgMood: float
    avgStress: float
    avgSleep: float
    checkinStreak: int
    dailySeries: list[DailySeriesPointDTO]
    aiInsights: list[AiStatInsightDTO]
