"""
Curated Clinical Self-Care & WHO/CBT Knowledge Cards (`rag_cards_vi.py`).
Generates `insightData` matching `ChatMessage['insightData']` in Frontend `src/types.ts`.
"""

from __future__ import annotations

from apps.accounts_consent.models import ClientProfile
from apps.ai_risk_engine.normalizer_vi import normalize_vietnamese
from apps.tracking.models import CheckInRecord

WHO_CBT_CARDS = [
    {
        "keywords": ("ngủ", "thức khuya", "mất ngủ", "trằn trọc", "giấc ngủ"),
        "referenceTitle": "Khuyến nghị Vệ sinh Giấc ngủ Sinh viên (WHO & CBT-I)",
        "referenceSnippet": (
            "Giữ giờ thức dậy cố định mỗi sáng kể cả cuối tuần, ngắt ánh sáng xanh 30 phút trước khi nằm xuống, "
            "và nếu trằn trọc quá 20 phút hãy ngồi dậy đọc vài trang giấy dưới đèn vàng thay vì ép mình ngủ."
        ),
    },
    {
        "keywords": ("thở", "4 7 8", "hồi hộp", "tim đập", "hoảng", "grounding"),
        "referenceTitle": "Cẩm nang WHO — Kỹ thuật Neo Cảm xúc 5-4-3-2-1 & Thở Phó giao cảm 4-7-8",
        "referenceSnippet": (
            "Khi dòng suy nghĩ cuốn đi quá nhanh, hãy gọi tên 5 vật bạn nhìn thấy, 4 bề mặt chạm tay, 3 âm thanh nghe được "
            "kết hợp chu kỳ hít vào 4s – giữ 7s – thở ra chậm 8s để hạ cortisol tự nhiên."
        ),
    },
    {
        "keywords": ("đồ án", "thi", "deadline", "bài tập", "áp lực", "học", "rớt môn", "điểm"),
        "referenceTitle": "Liệu pháp Nhận thức Hành vi (CBT) — Phân rã Nhiệm vụ & Gỡ Bẫy Cầu toàn",
        "referenceSnippet": (
            "Sự trì hoãn vì lo sợ thường bắt nguồn từ việc nhìn cả khối lượng lớn cùng lúc. Hãy áp dụng quy tắc 'Bước khởi động 5 phút' "
            "hoặc 1 phiên Pomodoro 25 phút cho phần việc nhỏ nhất để giải phóng vùng hạch hạnh nhân khỏi trạng thái đóng băng."
        ),
    },
    {
        "keywords": ("stress", "căng thẳng", "mệt", "quá tải", "cảm xúc", "năng lượng"),
        "referenceTitle": "Cẩm nang WHO 'Doing What Matters in Times of Stress' — Tháo móc Suy nghĩ",
        "referenceSnippet": (
            "Thay vì vật lộn chống lại cảm giác căng thẳng, hãy tập nhận diện và gọi tên nhẹ nhàng: 'Mình đang nhận thấy một suy nghĩ lo lắng xuất hiện', "
            "sau đó đưa sự chú ý quay lại hành động nhỏ có ý nghĩa ngay trước mắt."
        ),
    },
]


def build_insight_card(
    profile: ClientProfile,
    user_message: str,
    latest_checkin: dict | None = None,
) -> dict | None:
    norm = normalize_vietnamese(user_message).normalized_text
    selected_card = None
    for card in WHO_CBT_CARDS:
        if any(kw in norm for kw in card["keywords"]):
            selected_card = card
            break

    if not selected_card:
        return None

    records = list(CheckInRecord.objects.filter(profile=profile).order_by("-date", "-created_at_ms")[:7])
    ordered = list(reversed(records))

    if ordered:
        avg_stress = round(sum(r.stress for r in ordered) / len(ordered), 1)
        early_slice = ordered[: max(1, len(ordered) // 2)]
        late_slice = ordered[max(1, len(ordered) // 2) :] or ordered
        early_stress = round(sum(r.stress for r in early_slice) / len(early_slice), 1)
        late_stress = round(sum(r.stress for r in late_slice) / len(late_slice), 1)
        delta = round(late_stress - early_stress, 1)
        avg_sleep = round(sum(r.sleep_hours for r in ordered) / len(ordered), 1)
        daily_stress = [{"day": r.day_label, "val": int(r.stress)} for r in ordered]
    else:
        ci = latest_checkin or {}
        s_val = float(ci.get("stress", 5))
        slp_val = float(ci.get("sleepHours", 7.0))
        avg_stress = round(s_val, 1)
        early_stress = round(s_val, 1)
        delta = 0.0
        avg_sleep = round(slp_val, 1)
        daily_stress = [
            {"day": "T2", "val": int(s_val)},
            {"day": "T3", "val": max(1, int(s_val) - 1)},
            {"day": "T4", "val": int(s_val)},
        ]

    if delta < -0.1:
        delta_str = f"{delta:+.1f} (giảm nhẹ so với đầu tuần)"
    elif delta > 0.1:
        delta_str = f"{delta:+.1f} (tăng nhẹ so với đầu tuần)"
    else:
        delta_str = "Ổn định so với đầu tuần"

    sleep_status = "Đạt ngưỡng khuyến nghị" if avg_sleep >= 7.0 else "Hơi thiếu ngủ — cần bù giấc"

    return {
        "stressScore": f"{avg_stress}/10",
        "stressDelta": delta_str,
        "earlyWeekStress": f"{early_stress}/10",
        "avgSleep": f"{avg_sleep} giờ/đêm",
        "sleepStatus": sleep_status,
        "sleepGoal": "7.5 giờ/đêm",
        "dailyStress": daily_stress,
        "referenceTitle": selected_card["referenceTitle"],
        "referenceSnippet": selected_card["referenceSnippet"],
    }
