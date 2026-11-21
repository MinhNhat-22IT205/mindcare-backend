from __future__ import annotations

from datetime import timedelta
from django.db import transaction
from django.utils import timezone

from apps.accounts_consent.models import (
    DEFAULT_CLIENT_ID,
    ClientProfile,
    TrustedContactRecord,
    UserConsentProfile,
    make_pseudonym_id,
)

VN_WEEKDAY_SHORT = {0: "T2", 1: "T3", 2: "T4", 3: "T5", 4: "T6", 5: "T7", 6: "CN"}
VN_WEEKDAY_LONG = {
    0: "Thứ Hai",
    1: "Thứ Ba",
    2: "Thứ Tư",
    3: "Thứ Năm",
    4: "Thứ Sáu",
    5: "Thứ Bảy",
    6: "Chủ Nhật",
}

DEFAULT_SAFETY_PLAN_TEMPLATE = {
    "warning_signs": [
        "Thức trắng sau 1 giờ sáng kèm cảm giác ngực nặng và tự so sánh bản thân",
        "Ngại mở tin nhắn nhóm học tập, chỉ muốn thu mình trong phòng tối",
    ],
    "internal_coping": [
        "Thực hành 3 chu kỳ thở 4-7-8 trên MindCare và uống một cốc nước ấm",
        "Đi bộ chậm 10 phút quanh sân ký túc xá hoặc nghe tiếng mưa rơi",
    ],
    "safe_places_and_distractions": [
        "Thư viện trường tầng 2 hoặc quán cà phê yên tĩnh có ánh sáng tự nhiên",
        "Ngồi ở phòng sinh hoạt chung cùng bạn cùng phòng thay vì ở một mình",
    ],
    "trusted_people": [
        "Nguyễn Thị Lan (Bạn thân) — 0905123456",
        "Mẹ (Nguyễn Thị Mai) — 0912888889",
    ],
    "professional_contacts": [
        "Đường dây nóng Ngày Mai: 096 306 1414",
        "Tổng đài Quốc gia: 111 (miễn phí 24/7) | Tư vấn SKTT: 1900 1267 | Cấp cứu: 115",
    ],
    "safe_environment_steps": [
        "Cất toàn bộ vật sắc nhọn và thuốc dự trữ sang hộp khóa hoặc nhờ người nhà giữ",
        "Rời khỏi ban công/cầu thang cao khi đang hoảng loạn cấp tính",
    ],
}


def extract_client_id(request) -> str:
    raw = ""
    if hasattr(request, "headers"):
        raw = request.headers.get("X-MindCare-Client-Id", "") or request.headers.get("x-mindcare-client-id", "")
    if not raw and hasattr(request, "META"):
        raw = request.META.get("HTTP_X_MINDCARE_CLIENT_ID", "")
    cleaned = (raw or "").strip()[:120]
    return cleaned if cleaned else DEFAULT_CLIENT_ID


def get_or_init_profile(client_id: str) -> ClientProfile:
    cid = (client_id or DEFAULT_CLIENT_ID).strip()[:120] or DEFAULT_CLIENT_ID
    is_bare_client = cid.startswith(("cold-", "clean-", "bench-", "empty-"))

    alias = (
        "Minh Nguyễn (SV Năm 4 CNTT)"
        if cid == DEFAULT_CLIENT_ID
        else f"Sinh viên ({make_pseudonym_id(cid)})"
    )

    profile, _ = ClientProfile.objects.get_or_create(
        client_id=cid,
        defaults={
            "pseudonym_id": make_pseudonym_id(cid),
            "display_alias": alias,
            "role": "student",
        },
    )

    UserConsentProfile.objects.get_or_create(
        profile=profile,
        defaults={
            "allow_ai_checkin_personalize": True,
            "allow_ai_journal_context": False,
            "allow_auto_crisis_sos": False,
            "share_coarse_location_on_sos": False,
            "sos_feature_enabled": True,
        },
    )

    from apps.screening.models import SafetyPlanRecord

    SafetyPlanRecord.objects.get_or_create(
        profile=profile,
        defaults=DEFAULT_SAFETY_PLAN_TEMPLATE,
    )

    from apps.ai_risk_engine.services import get_or_create_risk_snapshot

    snap = get_or_create_risk_snapshot(profile)

    if not profile.seeded_demo_data:
        with transaction.atomic():
            locked = ClientProfile.objects.select_for_update().get(pk=profile.pk)
            if not locked.seeded_demo_data:
                if not is_bare_client:
                    _seed_demo_records(locked, snap)
                locked.seeded_demo_data = True
                locked.save(update_fields=["seeded_demo_data"])
                profile = locked

    return profile


def get_profile_from_request(request) -> ClientProfile:
    return get_or_init_profile(extract_client_id(request))


def _seed_demo_records(profile: ClientProfile, snap) -> None:
    from apps.tracking.models import CheckInRecord, JournalEntryRecord
    from apps.tracking.sentiment_vi import analyze_vietnamese_sentiment

    # 1. Seed 2 Trusted Contacts (never_share_journal enforced True)
    if not TrustedContactRecord.objects.filter(profile=profile).exists():
        TrustedContactRecord.objects.create(
            profile=profile,
            external_id="tc-1",
            name="Nguyễn Thị Lan",
            relationship="Bạn thân",
            phone="0905123456",
            avatar_letter="L",
            status="confirmed",
            permission_scope="Chỉ nhận tín hiệu hỗ trợ khi có SOS",
            only_on_sos=True,
            share_location=False,
            allow_auto_crisis_sos=True,
            never_share_journal=True,
        )
        TrustedContactRecord.objects.create(
            profile=profile,
            external_id="tc-2",
            name="Mẹ (Nguyễn Thị Mai)",
            relationship="Cha/Mẹ",
            phone="0912888889",
            avatar_letter="M",
            status="confirmed",
            permission_scope="Chỉ nhận tín hiệu ấm áp khi bạn chủ động bấm Yêu cầu hỗ trợ.",
            only_on_sos=True,
            share_location=False,
            allow_auto_crisis_sos=False,
            never_share_journal=True,
        )

    # 2. Seed 7 realistic daily Check-ins ending today (Asia/Ho_Chi_Minh)
    now_local = timezone.localtime(timezone.now())
    demo_series = [
        (0, "08:30", 6, "Nhẹ nhõm", 4, "Mức nhẹ", 7.5, "good", ["Biết ơn", "Tập trung", "Bình yên"],
         "Hoàn thành bài thuyết trình nhóm trên lớp trôi chảy, tối nay nghỉ ngơi sớm."),
        (1, "21:15", 7, "Khá tích cực", 4, "Mức nhẹ", 7.8, "refreshed", ["Bình yên", "Biết ơn"],
         "Đi dạo buổi tối quanh khuôn viên trường cùng bạn thân, đầu óc thư thái."),
        (2, "21:30", 6, "Ổn định", 5, "Vừa phải", 7.2, "good", ["Tập trung", "Hơi lo lắng"],
         "Nộp xong báo cáo tiến độ tuần, chuẩn bị tinh chỉnh phần thực nghiệm."),
        (3, "22:00", 6, "Khá ổn", 5, "Vừa phải", 7.0, "good", ["Học tập", "Hy vọng"],
         "Lên thư viện ôn bài và chia nhỏ đầu việc theo Pomodoro."),
        (4, "22:20", 5, "Hơi mỏi", 6, "Căng thẳng nhẹ", 6.2, "restless", ["Mệt mỏi", "Deadline"],
         "Thức hơi khuya sửa tài liệu nên sáng dậy hơi uể oải nhẹ."),
        (5, "21:10", 7, "Thư giãn", 4, "Mức nhẹ", 7.6, "good", ["Nhẹ nhõm", "Tập trung"],
         "Nghe tiếng mưa rơi và đọc sách 20 phút trước khi ngủ."),
        (6, "21:45", 6, "Ổn định", 5, "Vừa phải", 7.0, "good", ["Ổn định"],
         "Bắt đầu tuần mới với kế hoạch học tập rõ ràng."),
    ]

    if not CheckInRecord.objects.filter(profile=profile).exists():
        for days_ago, t_str, mood, mood_lbl, stress, stress_lbl, slp, slp_q, emos, note in demo_series:
            d_obj = (now_local - timedelta(days=days_ago)).date()
            d_iso = d_obj.isoformat()
            d_lbl = VN_WEEKDAY_SHORT[d_obj.weekday()]
            ts_ms = int((now_local - timedelta(days=days_ago)).timestamp() * 1000)
            sent = analyze_vietnamese_sentiment(note, self_mood_score=mood)
            rec = CheckInRecord(
                profile=profile,
                external_id=f"ci-seed-{days_ago}",
                date=d_iso,
                day_label=d_lbl,
                time=t_str,
                mood=mood,
                mood_label=mood_lbl,
                stress=stress,
                stress_label=stress_lbl,
                sleep_hours=slp,
                sleep_quality=slp_q,
                emotions=emos,
                note_negativity=sent.negativity_index,
                computed_mood_risk=0.14 if stress <= 4 else 0.22,
                allow_ai_personalize=True,
                created_at_ms=ts_ms,
            )
            rec.set_note(note)
            rec.save()

    # 3. Seed 3 AES-256-GCM Encrypted Journals
    if not JournalEntryRecord.objects.filter(profile=profile).exists():
        demo_journals = [
            (
                "j-seed-1",
                0,
                "21:40",
                "Một bước nhỏ hoàn thành bài đồ án tốt nghiệp",
                "Hôm nay mình đã hoàn thành một phần bài tập đồ án tốt nghiệp. Dù vẫn hơi lo về hạn nộp cuối tháng, "
                "nhưng cảm thấy nhẹ nhõm hơn nhiều khi chia nhỏ việc và đi dạo dưới tán cây.",
                "Nhẹ nhõm",
                7,
                "/assets/aistudio/journal_thumb_plant.png",
            ),
            (
                "j-seed-2",
                1,
                "22:15",
                "Dành buổi tối trọn vẹn cho bản thân",
                "Tối nay mình tắt điện thoại sớm, pha trà ấm và đọc vài trang sách trước khi đi ngủ. "
                "Cảm giác lắng đọng và bình yên thực sự.",
                "Thư giãn",
                7,
                "/assets/aistudio/journal_thumb_tea.png",
            ),
            (
                "j-seed-3",
                3,
                "16:30",
                "Buổi thuyết trình nhóm thành công",
                "Buổi thuyết trình nhóm khá ổn định và trôi chảy. Cảm giác nhẹ nhõm và biết ơn vì mọi người đều hỗ trợ nhau rất tử tế.",
                "Học tập",
                6,
                None,
            ),
        ]
        for j_id, days_ago, t_str, title, content, tag, m_score, img_url in demo_journals:
            dt_target = now_local - timedelta(days=days_ago)
            w_long = VN_WEEKDAY_LONG[dt_target.weekday()]
            date_label = f"{w_long}, {dt_target.strftime('%d/%m/%Y')}"
            sent = analyze_vietnamese_sentiment(content, self_mood_score=m_score)
            entry = JournalEntryRecord(
                profile=profile,
                external_id=j_id,
                date=date_label,
                day_label=w_long,
                time=t_str,
                title=title,
                tag=tag,
                mood_score=m_score,
                ai_estimate_label=sent.ai_estimate_label,
                ai_estimate_score=sent.ai_estimate_score,
                ai_bar_width=sent.ai_bar_width,
                negativity_index=sent.negativity_index,
                allow_ai_ref=False,
                created_at_ms=int(dt_target.timestamp() * 1000),
                image_url=img_url,
            )
            entry.set_plaintext_content(content)
            entry.save()

    # Initialize calm baseline snapshot matching API contract example
    now_iso = timezone.now().isoformat()
    snap.r_inst = 0.12
    snap.r_eff = 0.12
    snap.current_tier = "L0"
    snap.channel_raw_scores = {"chat": 0.05, "clin": 0.0, "mood": 0.12, "behav": 0.0}
    snap.channel_timestamps = {"chat": now_iso, "mood": now_iso}
    snap.channel_scores = {"chat": 0.05, "clin": 0.0, "mood": 0.12, "behav": 0.0}
    snap.channel_details = {"mood": "Tâm trạng ổn định", "chat": "Trò chuyện bình thường"}
    snap.attribution = [
        {
            "factor": "Check-in & Nhật ký",
            "key": "mood",
            "contribution": 0.12,
            "detail": "Tâm trạng ổn định",
        }
    ]
    snap.save()
