from __future__ import annotations

from datetime import date, timedelta
import time
import uuid
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router

from apps.accounts_consent.services import get_profile_from_request
from apps.ai_risk_engine.services import record_mood_tracking_signal
from apps.tracking.models import CheckInRecord, JournalEntryRecord
from apps.tracking.schemas import (
    CheckInDTO,
    CheckInInput,
    CheckInSaveResponseDTO,
    JournalEntryDTO,
    JournalEntryInput,
    JournalSaveResponseDTO,
    StatsOverviewDTO,
)
from apps.tracking.sentiment_vi import analyze_vietnamese_sentiment, detect_nocturnal_window

router = Router(tags=["Tracking: Check-in, Encrypted Journal & Stats"])


def _serialize_checkin(rec: CheckInRecord, requester_profile_id) -> dict:
    return {
        "id": rec.external_id,
        "date": rec.date,
        "dayLabel": rec.day_label,
        "time": rec.time,
        "mood": rec.mood,
        "moodLabel": rec.mood_label,
        "stress": rec.stress,
        "stressLabel": rec.stress_label,
        "sleepHours": float(rec.sleep_hours),
        "sleepQuality": rec.sleep_quality,
        "emotions": list(rec.emotions or []),
        "note": rec.get_note(requester_profile_id=requester_profile_id),
        "allowAiPersonalize": bool(rec.allow_ai_personalize),
        "createdAt": int(rec.created_at_ms),
    }


def _serialize_journal(rec: JournalEntryRecord, requester_profile_id) -> dict:
    return {
        "id": rec.external_id,
        "date": rec.date,
        "dayLabel": rec.day_label,
        "time": rec.time,
        "title": rec.title,
        "content": rec.get_decrypted_content(requester_profile_id=requester_profile_id),
        "tag": rec.tag,
        "moodScore": rec.mood_score,
        "aiEstimateLabel": rec.ai_estimate_label,
        "aiEstimateScore": rec.ai_estimate_score,
        "aiBarWidth": rec.ai_bar_width,
        "isEncrypted": True,
        "allowAiRef": bool(rec.allow_ai_ref),
        "createdAt": int(rec.created_at_ms),
        "imageUrl": rec.image_url,
    }


@router.get("/checkins", response=list[CheckInDTO])
def list_checkins(request):
    profile = get_profile_from_request(request)
    records = CheckInRecord.objects.filter(profile=profile).order_by("-date", "-created_at_ms")[:60]
    return [_serialize_checkin(r, profile.id) for r in records]


@router.post("/checkins", response=CheckInSaveResponseDTO)
def create_or_update_checkin(request, payload: CheckInInput):
    profile = get_profile_from_request(request)
    now_ms = payload.createdAt if payload.createdAt is not None else int(time.time() * 1000)
    ext_id = (payload.id or f"ci-{now_ms}").strip()

    mood_val = max(1, min(10, int(payload.mood)))
    stress_val = max(0, min(10, int(payload.stress)))
    sleep_val = max(0.0, min(16.0, float(payload.sleepHours)))

    # Collect 5 most recent prior check-in mood risks BEFORE saving current one (for Paradoxical Calm detection)
    prior_risks = list(
        CheckInRecord.objects.filter(profile=profile)
        .exclude(external_id=ext_id)
        .order_by("-date", "-created_at_ms")
        .values_list("computed_mood_risk", flat=True)[:6]
    )

    sent = analyze_vietnamese_sentiment(payload.note or "", self_mood_score=mood_val)
    is_nocturnal, hhmm = detect_nocturnal_window(time_str=payload.time)

    risk_dto, x_mood = record_mood_tracking_signal(
        profile=profile,
        mood_1_to_10=mood_val,
        stress_0_to_10=stress_val,
        sleep_hours=sleep_val,
        journal_negativity=sent.negativity_index,
        prior_mood_risks=prior_risks,
        closure_cue_detected=sent.closure_cue_detected,
        nocturnal_detected=is_nocturnal,
        nocturnal_hhmm=hhmm,
        source_label="CHECKIN",
    )

    rec, _ = CheckInRecord.objects.update_or_create(
        profile=profile,
        external_id=ext_id,
        defaults={
            "date": payload.date,
            "day_label": payload.dayLabel,
            "time": payload.time,
            "mood": mood_val,
            "mood_label": payload.moodLabel,
            "stress": stress_val,
            "stress_label": payload.stressLabel,
            "sleep_hours": sleep_val,
            "sleep_quality": payload.sleepQuality or "good",
            "emotions": payload.emotions or [],
            "note_negativity": sent.negativity_index,
            "computed_mood_risk": x_mood,
            "allow_ai_personalize": payload.allowAiPersonalize,
            "created_at_ms": now_ms,
        },
    )
    rec.set_note(payload.note or "")
    rec.save()

    return {
        "checkIn": _serialize_checkin(rec, profile.id),
        "riskSnapshot": risk_dto,
    }


@router.get("/journals", response=list[JournalEntryDTO])
def list_journals(request):
    profile = get_profile_from_request(request)
    entries = JournalEntryRecord.objects.filter(profile=profile).order_by("-created_at_ms")[:60]
    return [_serialize_journal(j, profile.id) for j in entries]


def _save_journal_common(profile, ext_id: str, payload: JournalEntryInput) -> dict:
    now_ms = payload.createdAt if payload.createdAt is not None else int(time.time() * 1000)
    mood_val = max(1, min(10, int(payload.moodScore)))

    combined_text = f"{payload.title}. {payload.content}".strip()
    sent = analyze_vietnamese_sentiment(combined_text, self_mood_score=mood_val)
    is_nocturnal, hhmm = detect_nocturnal_window(time_str=payload.time)

    # Blend with latest check-in stress/sleep if available
    latest_ci = CheckInRecord.objects.filter(profile=profile).order_by("-date", "-created_at_ms").first()
    eff_stress = latest_ci.stress if latest_ci else max(1, 10 - sent.ai_estimate_score)
    eff_sleep = latest_ci.sleep_hours if latest_ci else 7.0
    prior_risks = list(
        CheckInRecord.objects.filter(profile=profile)
        .order_by("-date", "-created_at_ms")
        .values_list("computed_mood_risk", flat=True)[:6]
    )

    risk_dto, _ = record_mood_tracking_signal(
        profile=profile,
        mood_1_to_10=sent.ai_estimate_score,
        stress_0_to_10=eff_stress,
        sleep_hours=eff_sleep,
        journal_negativity=sent.negativity_index,
        prior_mood_risks=prior_risks,
        closure_cue_detected=sent.closure_cue_detected,
        nocturnal_detected=is_nocturnal,
        nocturnal_hhmm=hhmm,
        source_label="JOURNAL",
    )

    rec, _ = JournalEntryRecord.objects.update_or_create(
        profile=profile,
        external_id=ext_id,
        defaults={
            "date": payload.date,
            "day_label": payload.dayLabel,
            "time": payload.time,
            "title": payload.title,
            "tag": payload.tag,
            "mood_score": mood_val,
            "ai_estimate_label": sent.ai_estimate_label,
            "ai_estimate_score": sent.ai_estimate_score,
            "ai_bar_width": sent.ai_bar_width,
            "negativity_index": sent.negativity_index,
            "is_encrypted": True,
            "allow_ai_ref": payload.allowAiRef,
            "created_at_ms": now_ms,
            "image_url": payload.imageUrl,
        },
    )
    rec.set_plaintext_content(payload.content)
    rec.save()

    return {
        "journal": _serialize_journal(rec, profile.id),
        "riskSnapshot": risk_dto,
    }


@router.post("/journals", response=JournalSaveResponseDTO)
def create_journal(request, payload: JournalEntryInput):
    profile = get_profile_from_request(request)
    ext_id = (payload.id or f"j-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}").strip()
    return _save_journal_common(profile, ext_id, payload)


@router.put("/journals/{journal_id}", response=JournalSaveResponseDTO)
def update_journal(request, journal_id: str, payload: JournalEntryInput):
    profile = get_profile_from_request(request)
    get_object_or_404(JournalEntryRecord, profile=profile, external_id=journal_id)
    return _save_journal_common(profile, journal_id, payload)


@router.delete("/journals/{journal_id}")
def delete_journal(request, journal_id: str):
    profile = get_profile_from_request(request)
    rec = get_object_or_404(JournalEntryRecord, profile=profile, external_id=journal_id)
    rec.delete()
    return {"deleted": True}


def _compute_checkin_streak(dates_iso: list[str]) -> int:
    parsed: set[date] = set()
    for d_str in dates_iso:
        try:
            parsed.add(date.fromisoformat(d_str[:10]))
        except Exception:
            continue
    if not parsed:
        return 0
    today = timezone.localtime(timezone.now()).date()
    yesterday = today - timedelta(days=1)
    if today in parsed:
        cursor = today
    elif yesterday in parsed:
        cursor = yesterday
    else:
        return 0
    streak = 0
    while cursor in parsed:
        streak += 1
        cursor = cursor - timedelta(days=1)
    return streak


@router.get("/stats/overview", response=StatsOverviewDTO)
def get_stats_overview(request):
    profile = get_profile_from_request(request)
    recent_records = list(CheckInRecord.objects.filter(profile=profile).order_by("-date", "-created_at_ms")[:14])

    if not recent_records:
        return {
            "avgMood": 0.0,
            "avgStress": 0.0,
            "avgSleep": 0.0,
            "checkinStreak": 0,
            "dailySeries": [],
            "aiInsights": [
                {
                    "title": "Bắt đầu chuỗi theo dõi cảm xúc",
                    "body": "Hãy thực hiện Check-in đầu tiên trong ngày để Mochi vẽ biểu đồ nhịp sinh học và mức năng lượng cá nhân cho bạn nhé.",
                    "tone": "supportive",
                }
            ],
        }

    # Deduplicate by date (keep latest per date) for daily series up to 7 days
    by_date: dict[str, CheckInRecord] = {}
    for rec in recent_records:
        if rec.date not in by_date:
            by_date[rec.date] = rec

    ordered_7 = sorted(by_date.values(), key=lambda r: r.date)[-7:]
    avg_mood = round(sum(r.mood for r in ordered_7) / len(ordered_7), 1)
    avg_stress = round(sum(r.stress for r in ordered_7) / len(ordered_7), 1)
    avg_sleep = round(sum(r.sleep_hours for r in ordered_7) / len(ordered_7), 1)
    streak = _compute_checkin_streak(list(by_date.keys()))

    daily_series = [
        {
            "date": r.date,
            "dayLabel": r.day_label,
            "mood": float(r.mood),
            "stress": float(r.stress),
            "sleepHours": round(float(r.sleep_hours), 1),
        }
        for r in ordered_7
    ]

    insights: list[dict] = []
    if avg_sleep < 6.5:
        insights.append(
            {
                "title": "Thiếu hụt giấc ngủ ảnh hưởng mức căng thẳng",
                "body": f"Trung bình giấc ngủ tuần qua của bạn là {avg_sleep}h (< 7h khuyến nghị), đi kèm mức stress trung bình {avg_stress}/10. Ngủ sớm thêm 30 phút tối nay sẽ giúp phục hồi rõ rệt.",
                "tone": "caution",
            }
        )
    else:
        insights.append(
            {
                "title": "Nhịp sinh học & Mức căng thẳng",
                "body": f"Bạn duy trì giấc ngủ trung bình {avg_sleep}h/đêm giúp điểm tâm trạng giữ ở mức {avg_mood}/10 và kiểm soát stress quanh {avg_stress}/10.",
                "tone": "supportive",
            }
        )

    if streak >= 3:
        insights.append(
            {
                "title": f"Chuỗi đồng hành {streak} ngày liên tiếp",
                "body": "Thói quen gọi tên cảm xúc đều đặn giúp bạn nhận diện sớm các dấu hiệu quá tải trước khi chúng tích tụ.",
                "tone": "positive",
            }
        )

    return {
        "avgMood": avg_mood,
        "avgStress": avg_stress,
        "avgSleep": avg_sleep,
        "checkinStreak": streak,
        "dailySeries": daily_series,
        "aiInsights": insights,
    }
