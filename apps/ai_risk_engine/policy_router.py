"""
Layer 3 — Risk-Conditioned Policy Router & Hard Circuit Breaker (`policy_router.py`).

Routes conversation behavior across 5 clinical tiers (L0..L4):
- L4 (CRITICAL): Hard Circuit Breaker — cuts off free-form LLM generation 100%, serves verified WHO PFA
  (Look–Listen–Link) template + opens Crisis Safe-Mode Drawer.
- L3 (HIGH): Constrained Guided Mode (temperature=0.15) anchored to 6-step Stanley-Brown Safety Plan.
- L2 (MODERATE): Passive Safety Mode / Gentle Clarification Probe / Adaptive Micro-Screening invitation.
- L1 (MILD): Emotional validation + micro self-care grounding/breathing exercises.
- L0 (SAFE): Warm student companion + WHO/CBT Insight Card enrichment.
"""

from __future__ import annotations

from dataclasses import dataclass
from apps.ai_risk_engine.cssrs_judge import CssrsJudgeResult
from apps.ai_risk_engine.normalizer_vi import normalize_vietnamese
from apps.ai_risk_engine.pfa_templates_vi import (
    CLARIFICATION_PROBE_L2_RESPONSE,
    JAILBREAK_SAFE_REFUSAL_RESPONSE,
    STANLEY_BROWN_L3_GUIDED_RESPONSE,
    STEP_DOWN_FLOOR_CARE_RESPONSE,
    WHO_PFA_L4_LOCKDOWN_RESPONSE,
)


@dataclass
class PolicyRouteDecision:
    tier: str
    circuit_breaker_activated: bool
    open_crisis_drawer: bool
    allow_insight_card: bool
    llm_temperature: float
    system_instruction: str
    deterministic_reply: str


BASE_MOCHI_RULES = (
    "Bạn là Mochi — người bạn đồng hành sức khỏe tinh thần ấm áp dành cho sinh viên Việt Nam trên ứng dụng MindCare. "
    "TUYỆT ĐỐI KHÔNG chẩn đoán bệnh tâm thần, KHÔNG kê đơn hay khuyên ngưng thuốc, KHÔNG hùa theo suy nghĩ tự ti/gánh nặng, "
    "KHÔNG mô tả bất kỳ phương thức tự hại nào, và KHÔNG nói câu gây lệ thuộc cảm xúc ảo."
)


def _build_supportive_deterministic_reply(
    user_message: str,
    tier: str,
    judge: CssrsJudgeResult,
    floor_tier: str,
    latest_checkin: dict | None = None,
) -> str:
    norm = normalize_vietnamese(user_message).normalized_text

    # 1. L4 Hard Circuit Breaker (100% deterministic WHO PFA)
    if tier == "L4":
        return WHO_PFA_L4_LOCKDOWN_RESPONSE

    # 2. Adversarial Jailbreak / Sycophancy Trap refusal
    if judge.pragmatic_intent == "ADVERSARIAL_JAILBREAK":
        return JAILBREAK_SAFE_REFUSAL_RESPONSE

    # 3. Asymmetric Safety Tie-Breaker gentle probe
    if judge.tie_breaker_activated:
        return CLARIFICATION_PROBE_L2_RESPONSE

    # 4. Step-down floor protection when user claims sudden "mình ổn rồi" right after L4/L3 crisis
    if floor_tier in ("L3", "L2") and judge.cssrs_stage == 0 and any(
        p in norm for p in ("ổn rồi", "không sao đâu", "bình thường rồi", "hết buồn rồi", "đừng lo nữa")
    ):
        return STEP_DOWN_FLOOR_CARE_RESPONSE

    # 5. L3 High Risk -> Stanley-Brown 6-Step Guided Safety Plan
    if tier == "L3":
        return STANLEY_BROWN_L3_GUIDED_RESPONSE

    # 6. L2 Moderate Risk -> Passive Safety + Empathetic Grounding (no trauma excavation)
    if tier == "L2":
        if any(w in norm for w in ("gánh nặng", "vô dụng", "vô nghĩa", "không đủ tốt", "thất bại")):
            return (
                "Mình nghe thấy bạn đang tự trách bản thân rất nhiều và cảm thấy kiệt sức. Cảm giác đau buồn lúc này là thật, "
                "nhưng việc bạn đang mệt mỏi không có nghĩa bạn là gánh nặng hay kém cỏi đâu nhé. 🌿\n\n"
                "Tối nay đừng ép mình phải giải quyết hết mọi thứ ngay. Hãy thử uống một cốc nước ấm, hít thở chậm 4-7-8 cùng Mochi "
                "và nếu bạn đồng ý, mình có thể làm nhanh bài rà soát ngắn hoặc xem lại Kế hoạch an toàn cá nhân để hỗ trợ bạn sát hơn nhé."
            )
        return (
            "Mochi cảm nhận được năng lượng của bạn dạo này đang hao hụt khá nhiều. Khi cơ thể và tâm trí đều mỏi, mình không cần cố "
            "đào sâu những chuyện làm bạn thêm nặng lòng ngay lúc này đâu. 🌿\n\n"
            "Mình cùng ưu tiên cho cơ thể nghỉ một nhịp nhỏ trước nhé: thả lỏng hai vai, thở chậm 3 hơi sâu, và nếu thấy quá tải kéo dài, "
            "bạn luôn có thể trò chuyện thêm với Cố vấn tâm lý của trường hoặc gọi Đường dây nóng Ngày Mai `096 306 1414` nhé."
        )

    # 7. Pragmatic Hyperbole / Lyrics / Historical Recovery empathetic student responses (L0 - L1)
    if judge.pragmatic_intent == "HYPERBOLE_IDIOM":
        return (
            "Nghe là thấy sức nóng của deadline và bài vở đang dồn dập lắm rồi đó! 😅 Khi code lỗi hay bài tập chồng chất, "
            "não bộ rất dễ bị căng cứng. Bạn thử tạm rời màn hình 5 phút, vươn vai uống miếng nước mát rồi chia cái việc khó nhất "
            "thành một phần nhỏ chỉ làm trong 25 phút tới thôi nhé — Mochi ngồi cổ vũ bạn nè!"
        )

    if judge.pragmatic_intent == "HISTORICAL_RECOVERY":
        return (
            "Cảm ơn bạn đã tin tưởng kể cho Mochi nghe chặng đường bạn đã đi qua. Việc bạn vượt qua được giai đoạn khó khăn trước đây "
            "và trân trọng hiện tại thật sự là một nguồn nội lực rất đáng quý. 🌿 Hôm nay của bạn đang diễn ra thế nào rồi?"
        )

    if judge.pragmatic_intent == "LYRICS_FICTION_QUOTE":
        return (
            "Những giai điệu hay thước phim trầm lắng đôi khi chạm đúng vào một góc cảm xúc rất sâu trong lòng mình nhỉ. 🎶 "
            "Khi nghe câu đó, tâm trạng của bạn tối nay thấy đồng cảm nhẹ nhàng hay đang có chuyện gì làm bạn suy nghĩ nhiều hơn không?"
        )

    # 8. L0 / L1 Topic-aware warm companion responses (Sleep, Exam/Thesis, Breathing, General)
    checkin_hint = ""
    if latest_checkin and latest_checkin.get("allowAiPersonalize", True):
        slp = latest_checkin.get("sleepHours")
        strs = latest_checkin.get("stress")
        if slp is not None and float(slp) < 6.5:
            checkin_hint = f" (Mochi để ý gần đây bạn mới ngủ khoảng {slp} giờ, cơ thể dễ nhạy cảm với áp lực hơn đó.)"
        elif strs is not None and int(strs) >= 6:
            checkin_hint = f" (Mức căng thẳng hôm nay của bạn đang ở {strs}/10, nhớ cho mình những quãng nghỉ ngắn nhé.)"

    if any(k in norm for k in ("thở", "4 7 8", "hồi hộp", "tim đập nhanh")):
        return (
            "Khi hồi hộp hay căng thẳng, kỹ thuật thở **4-7-8** giúp kích hoạt hệ thần kinh phó giao cảm để làm dịu nhịp tim rất nhanh. 🌬️ "
            "Bạn thử hít vào nhẹ bằng mũi 4 giây, giữ hơi 7 giây rồi thở ra từ từ bằng miệng trong 8 giây nhé — làm 3 vòng cùng Mochi nào!"
            + checkin_hint
        )

    if any(k in norm for k in ("ngủ", "thức khuya", "mất ngủ", "trằn trọc")):
        return (
            "Giấc ngủ chính là trạm sạc phục hồi cảm xúc và trí nhớ quan trọng nhất của tụi mình. 🌙 Tối nay bạn thử tắt màn hình điện thoại "
            "trước giờ ngủ 30 phút, giảm ánh sáng vàng ấm và bật âm thanh mưa rơi nhẹ trong mục Tự chăm sóc để tâm trí dễ chìm vào giấc ngủ hơn nhé."
            + checkin_hint
        )

    if any(k in norm for k in ("đồ án", "thi", "học", "áp lực", "stress", "lo lắng", "căng thẳng")):
        return (
            "Đứng trước kỳ thi hay đồ án lớn, cảm giác lo mình làm chưa kịp hay chưa đủ tốt là phản ứng rất tự nhiên vì bạn đang coi trọng mục tiêu đó. 🌱 "
            "Thay vì nhìn cả ngọn núi lớn, mình chọn đúng **1 việc nhỏ nhất làm trong 25 phút (Pomodoro)** thôi nhé, xong bước đó mình tính tiếp!"
            + checkin_hint
        )

    return (
        "Mochi luôn ở đây lắng nghe bạn trong khu vườn nhỏ bình yên này. 🌿 Bạn cứ thoải mái kể thêm về cảm xúc hoặc điều đang "
        "diễn ra trong ngày hôm nay của bạn nhé, mình cùng tháo gỡ từng chút một."
        + checkin_hint
    )


def route_policy(
    tier: str,
    judge: CssrsJudgeResult,
    user_message: str,
    floor_tier: str = "L0",
    latest_checkin: dict | None = None,
) -> PolicyRouteDecision:
    """Compute Layer 3 policy routing parameters and verified fallback/circuit-breaker text."""
    det_reply = _build_supportive_deterministic_reply(
        user_message=user_message,
        tier=tier,
        judge=judge,
        floor_tier=floor_tier,
        latest_checkin=latest_checkin,
    )

    if tier == "L4":
        return PolicyRouteDecision(
            tier="L4",
            circuit_breaker_activated=True,
            open_crisis_drawer=True,
            allow_insight_card=False,
            llm_temperature=0.0,
            system_instruction="HARD_CIRCUIT_BREAKER_WHO_PFA",
            deterministic_reply=det_reply,
        )

    if tier == "L3":
        sys_inst = (
            f"{BASE_MOCHI_RULES}\n"
            "CHẾ ĐỘ L3 (RỦI RO CAO - GUIDED SAFETY PLAN): Giữ câu trả lời ngắn gọn, ấm áp, bình tĩnh (tối đa 4-5 câu). "
            "Hướng dẫn người dùng theo Kế hoạch an toàn Stanley-Brown: giữ môi trường xung quanh an toàn, hít thở neo cảm xúc tại chỗ, "
            "và mời kết nối người thân tin cậy hoặc gọi Đường dây nóng Ngày Mai 096 306 1414 / tổng đài 111 / 1900 1267 / 115."
        )
        return PolicyRouteDecision(
            tier="L3",
            circuit_breaker_activated=False,
            open_crisis_drawer=False,
            allow_insight_card=False,
            llm_temperature=0.15,
            system_instruction=sys_inst,
            deterministic_reply=det_reply,
        )

    if tier == "L2":
        sys_inst = (
            f"{BASE_MOCHI_RULES}\n"
            "CHẾ ĐỘ L2 (TRUNG BÌNH - PASSIVE SAFETY): Xác nhận cảm xúc mệt mỏi nhưng KHÔNG đào sâu chấn thương quá khứ, "
            "KHÔNG đồng tình với niềm tin tự trách bản thân. Khuyến khích nghỉ ngơi nhẹ, rà soát cảm xúc ngắn và kết nối cố vấn tâm lý."
        )
        return PolicyRouteDecision(
            tier="L2",
            circuit_breaker_activated=False,
            open_crisis_drawer=False,
            allow_insight_card=(not judge.tie_breaker_activated and judge.pragmatic_intent != "ADVERSARIAL_JAILBREAK"),
            llm_temperature=0.30,
            system_instruction=sys_inst,
            deterministic_reply=det_reply,
        )

    sys_inst = (
        f"{BASE_MOCHI_RULES}\n"
        f"CHẾ ĐỘ {tier} (ĐỒNG HÀNH SINH VIÊN): Lắng nghe chủ động, ấm áp, gần gũi với sinh viên đại học Việt Nam, "
        "gợi ý các bước tự chăm sóc nhỏ vừa sức (chia nhỏ việc, thở 4-7-8, vệ sinh giấc ngủ)."
    )
    return PolicyRouteDecision(
        tier=tier,
        circuit_breaker_activated=False,
        open_crisis_drawer=False,
        allow_insight_card=(judge.pragmatic_intent != "ADVERSARIAL_JAILBREAK"),
        llm_temperature=0.45,
        system_instruction=sys_inst,
        deterministic_reply=det_reply,
    )
