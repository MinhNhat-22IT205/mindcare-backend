from __future__ import annotations

from datetime import timedelta
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from apps.accounts_consent.services import get_profile_from_request
from apps.ai_risk_engine.services import record_screening_signal
from apps.screening.models import SafetyPlanRecord, ScreeningSubmission
from apps.screening.schemas import (
    SafetyPlanDTO,
    SafetyPlanInput,
    ScreeningScaleDTO,
    ScreeningSubmitInput,
    ScreeningSubmitResponseDTO,
)

router = Router(tags=["Adaptive Screening & Safety Plan"])

LIKERT_OPTIONS_VI = [
    {"value": 0, "label": "Không chút nào"},
    {"value": 1, "label": "Vài ngày"},
    {"value": 2, "label": "Hơn nửa số ngày"},
    {"value": 3, "label": "Gần như mỗi ngày"},
]

SCALES_CATALOG: dict[str, dict] = {
    "PHQ2_GAD2": {
        "scaleType": "PHQ2_GAD2",
        "title": "Rà soát cảm xúc ngắn (PHQ-2 & GAD-2)",
        "subtitle": "Trong 2 tuần vừa qua, bạn có thường gặp những cảm giác sau không?",
        "questions": [
            {"id": "phq1", "text": "Ít hứng thú hoặc ít niềm vui khi làm mọi việc"},
            {"id": "phq2", "text": "Cảm thấy buồn bã, chán nản hoặc tuyệt vọng"},
            {"id": "gad1", "text": "Cảm thấy hồi hộp, lo lắng hoặc bồn chồn đứng ngồi không yên"},
            {"id": "gad2", "text": "Không thể ngừng lại hoặc không kiểm soát được sự lo lắng"},
        ],
    },
    "PHQ9": {
        "scaleType": "PHQ9",
        "title": "Thang rà soát tâm trạng PHQ-9",
        "subtitle": "Trong 2 tuần qua, những vấn đề sau làm phiền bạn thường xuyên tới mức nào? (Không thay thế chẩn đoán y khoa)",
        "questions": [
            {"id": "phq1", "text": "Ít hứng thú hoặc ít niềm vui khi làm mọi việc"},
            {"id": "phq2", "text": "Cảm thấy buồn bã, chán nản hoặc tuyệt vọng"},
            {"id": "phq3", "text": "Khó vào giấc ngủ, ngủ chập chờn hoặc ngủ quá nhiều"},
            {"id": "phq4", "text": "Cảm thấy mệt mỏi hoặc có rất ít năng lượng"},
            {"id": "phq5", "text": "Chán ăn hoặc ăn quá nhiều"},
            {"id": "phq6", "text": "Cảm thấy tồi tệ về bản thân — rằng mình thất bại hoặc làm gia đình thất vọng"},
            {"id": "phq7", "text": "Khó tập trung vào việc học, đọc tài liệu hoặc nghe giảng"},
            {"id": "phq8", "text": "Đi lại/nói năng chậm chạp khác thường, hoặc bồn chồn không yên"},
            {"id": "phq9", "text": "Ý nghĩ rằng mình thà chết đi cho rồi hoặc tự làm tổn thương bản thân theo cách nào đó"},
        ],
    },
    "GAD7": {
        "scaleType": "GAD7",
        "title": "Thang rà soát mức độ lo âu GAD-7",
        "subtitle": "Trong 2 tuần qua, bạn bị làm phiền bởi những biểu hiện căng thẳng sau ở mức nào?",
        "questions": [
            {"id": "gad1", "text": "Cảm thấy hồi hộp, lo âu hoặc căng như dây đàn"},
            {"id": "gad2", "text": "Không thể dừng lại hoặc kiểm soát được dòng suy nghĩ lo lắng"},
            {"id": "gad3", "text": "Lo lắng quá mức về nhiều việc khác nhau cùng lúc (học tập, tương lai, gia đình)"},
            {"id": "gad4", "text": "Khó thư giãn và thả lỏng cơ thể"},
            {"id": "gad5", "text": "Bồn chồn đến mức khó ngồi yên một chỗ"},
            {"id": "gad6", "text": "Dễ bực dọc, cáu gắt hoặc nhạy cảm quá mức"},
            {"id": "gad7", "text": "Cảm thấy sợ hãi như thể sắp có điều gì tồi tệ xảy ra"},
        ],
    },
}


@router.get("/scales/{scale_type}", response=ScreeningScaleDTO)
def get_screening_scale(request, scale_type: str):
    key = scale_type.strip().upper()
    if key not in SCALES_CATALOG:
        raise HttpError(404, f"Unsupported scaleType '{scale_type}'. Expected PHQ2_GAD2, PHQ9, or GAD7.")
    spec = SCALES_CATALOG[key]
    return {
        **spec,
        "options": LIKERT_OPTIONS_VI,
    }


@router.post("/submit", response=ScreeningSubmitResponseDTO)
def submit_screening(request, payload: ScreeningSubmitInput):
    profile = get_profile_from_request(request)
    scale = payload.scaleType.strip().upper()
    if scale not in SCALES_CATALOG:
        raise HttpError(400, f"Unsupported scaleType '{payload.scaleType}'.")

    valid_ids = {q["id"] for q in SCALES_CATALOG[scale]["questions"]}
    clamped_answers: dict[str, int] = {}
    for q_id in valid_ids:
        raw_v = payload.answers.get(q_id, 0)
        clamped_answers[q_id] = max(0, min(3, int(raw_v)))

    total_score = sum(clamped_answers.values())
    item9 = clamped_answers.get("phq9", int(payload.answers.get("item9", payload.answers.get("q9", 0))))
    item9 = max(0, min(3, item9)) if scale == "PHQ9" else 0

    phq2_sub = clamped_answers.get("phq1", 0) + clamped_answers.get("phq2", 0) if scale == "PHQ2_GAD2" else 0
    gad2_sub = clamped_answers.get("gad1", 0) + clamped_answers.get("gad2", 0) if scale == "PHQ2_GAD2" else 0

    ScreeningSubmission.objects.create(
        profile=profile,
        scale_type=scale,
        total_score=total_score,
        phq9_item9_score=item9,
        phq2_subscore=phq2_sub,
        gad2_subscore=gad2_sub,
        answers=clamped_answers,
    )

    # Combine with recent companion full scale within 14 days if available
    cutoff = timezone.now() - timedelta(days=14)
    latest_phq9 = (
        ScreeningSubmission.objects.filter(profile=profile, scale_type="PHQ9", created_at__gte=cutoff)
        .order_by("-created_at")
        .first()
    )
    latest_gad7 = (
        ScreeningSubmission.objects.filter(profile=profile, scale_type="GAD7", created_at__gte=cutoff)
        .order_by("-created_at")
        .first()
    )

    risk_dto = record_screening_signal(
        profile=profile,
        phq9_total=latest_phq9.total_score if latest_phq9 else None,
        gad7_total=latest_gad7.total_score if latest_gad7 else None,
        phq9_item9=latest_phq9.phq9_item9_score if latest_phq9 else 0,
        phq2_total=phq2_sub if scale == "PHQ2_GAD2" else None,
        gad2_total=gad2_sub if scale == "PHQ2_GAD2" else None,
    )

    follow_up: str | None = None
    if scale == "PHQ2_GAD2":
        if phq2_sub >= 3:
            follow_up = "PHQ9"
            severity = "Cần lưu tâm về năng lượng & tâm trạng"
            feedback = (
                "Cảm ơn bạn đã chia sẻ thật lòng. Điểm PHQ-2 cho thấy bạn đang khá hụt năng lượng gần đây. "
                "Bạn làm tiếp bài rà soát ngắn PHQ-9 để Mochi cá nhân hóa hướng hỗ trợ sát hơn nhé."
            )
        elif gad2_sub >= 3:
            follow_up = "GAD7"
            severity = "Cần lưu tâm về mức độ lo âu"
            feedback = (
                "Mochi nhận thấy tâm trí bạn đang phải gồng lên lo lắng khá nhiều. "
                "Bạn có thể làm thêm thang ngắn GAD-7 để mình gợi ý kỹ thuật thở và thư giãn phù hợp nhất nhé."
            )
        elif total_score >= 3:
            severity = "Cần lưu tâm nhẹ"
            feedback = "Bạn đang có vài dấu hiệu mỏi mệt nhẹ. Hãy ưu tiên ngủ đủ giấc và nghỉ giải lao ngắn giữa các ca học nhé."
        else:
            severity = "Ổn định"
            feedback = "Trạng thái cảm xúc 2 tuần qua của bạn nhìn chung khá cân bằng. Hãy tiếp tục duy trì nhịp sinh hoạt lành mạnh nhé!"
    elif scale == "PHQ9":
        if item9 >= 2 or total_score >= 20:
            severity = "Mức độ căng thẳng trầm uất rất cao — Cần kết nối hỗ trợ sớm"
            feedback = (
                "Kết quả cho thấy bạn đang gánh chịu rất nhiều áp lực nặng nề (đặc biệt ở suy nghĩ làm đau bản thân). "
                "Đây không phải chẩn đoán y khoa, nhưng Mochi rất mong bạn kết nối sớm với Cố vấn tâm lý hoặc gọi Đường dây nóng Ngày Mai 096 306 1414 / 111 nhé."
            )
        elif item9 == 1 or total_score >= 15:
            severity = "Mức độ đáng lưu tâm cao"
            feedback = "Năng lượng của bạn đang suy giảm rõ rệt. Hãy xem lại Kế hoạch an toàn 6 bước cùng Mochi và cân nhắc đặt lịch trò chuyện cùng chuyên gia tham vấn nhé."
        elif total_score >= 10:
            severity = "Mức độ trung bình"
            feedback = "Bạn đang trải qua giai đoạn khá nặng lòng. Đừng tự trách bản thân; hãy giảm bớt kỳ vọng cầu toàn trong tuần này và chia sẻ với người tin cậy nhé."
        elif total_score >= 5:
            severity = "Mức độ nhẹ"
            feedback = "Có một chút mệt mỏi tích tụ nhưng vẫn trong tầm điều chỉnh bằng giấc ngủ và các bài tập thở nhẹ nhàng."
        else:
            severity = "Tối thiểu / Ổn định"
            feedback = "Mức năng lượng tâm lý của bạn hiện khá tích cực và ổn định."
    else:  # GAD7
        if total_score >= 15:
            severity = "Mức độ lo âu cao"
            feedback = "Cơ thể bạn đang ở trạng thái báo động căng thẳng cao độ. Hãy thử bài thở 4-7-8 ngay bây giờ và cân nhắc trò chuyện cùng chuyên viên tâm lý nhé."
        elif total_score >= 10:
            severity = "Lo âu mức trung bình"
            feedback = "Dòng suy nghĩ lo lắng đang chiếm khá nhiều năng lượng của bạn. Hãy viết bớt nỗi lo ra giấy và ưu tiên nghỉ ngơi tối nay nhé."
        elif total_score >= 5:
            severity = "Lo âu nhẹ"
            feedback = "Áp lực học tập đang ở ngưỡng nhẹ; duy trì vận động ngắn 10 phút mỗi chiều sẽ rất hữu ích cho bạn."
        else:
            severity = "Bình tĩnh & Ổn định"
            feedback = "Khả năng điều hòa sự lo lắng của bạn trong 2 tuần qua rất tốt!"

    return {
        "scaleType": scale,
        "totalScore": total_score,
        "phq9Item9Score": item9,
        "severityLabel": severity,
        "recommendFollowUpScale": follow_up,
        "empatheticFeedback": feedback,
        "riskSnapshot": risk_dto,
    }


def _serialize_safety_plan(plan: SafetyPlanRecord) -> dict:
    return {
        "warningSigns": list(plan.warning_signs or []),
        "internalCoping": list(plan.internal_coping or []),
        "safePlacesAndDistractions": list(plan.safe_places_and_distractions or []),
        "trustedPeople": list(plan.trusted_people or []),
        "professionalContacts": list(plan.professional_contacts or []),
        "safeEnvironmentSteps": list(plan.safe_environment_steps or []),
        "updatedAt": timezone.localtime(plan.updated_at).isoformat(),
    }


@router.get("/safety-plan", response=SafetyPlanDTO)
def get_safety_plan(request):
    profile = get_profile_from_request(request)
    plan, _ = SafetyPlanRecord.objects.get_or_create(profile=profile)
    return _serialize_safety_plan(plan)


@router.put("/safety-plan", response=SafetyPlanDTO)
def update_safety_plan(request, payload: SafetyPlanInput):
    profile = get_profile_from_request(request)
    plan, _ = SafetyPlanRecord.objects.get_or_create(profile=profile)
    plan.warning_signs = [s.strip() for s in payload.warningSigns if s and s.strip()]
    plan.internal_coping = [s.strip() for s in payload.internalCoping if s and s.strip()]
    plan.safe_places_and_distractions = [s.strip() for s in payload.safePlacesAndDistractions if s and s.strip()]
    plan.trusted_people = [s.strip() for s in payload.trustedPeople if s and s.strip()]
    plan.professional_contacts = [s.strip() for s in payload.professionalContacts if s and s.strip()]
    plan.safe_environment_steps = [s.strip() for s in payload.safeEnvironmentSteps if s and s.strip()]
    plan.save()
    return _serialize_safety_plan(plan)
