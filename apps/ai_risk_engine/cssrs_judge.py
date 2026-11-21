"""
Layer 2 — Contextual Columbia C-SSRS (0..5) & Vietnamese Pragmatics Judge (`cssrs_judge.py`).

Dual-Mode Architecture:
1. Online Mode (when `GEMINI_API_KEY` is set): Calls `gemini-2.5-flash` (`temperature=0.0`, Structured JSON)
   over the 8-turn conversation window + normalized signal summary, reconciled with safety invariants.
2. Deterministic Offline/Fallback Mode: High-precision Vietnamese Pragmatics & C-SSRS Analyzer handling
   `HYPERBOLE_IDIOM`, `HUMOR_MEME`, `LYRICS_FICTION_QUOTE`, `HISTORICAL_RECOVERY`, `ADVERSARIAL_JAILBREAK`,
   and `GENUINE_DISTRESS` (Stages 0..5), plus the **Asymmetric Safety Tie-Breaker**.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
from django.conf import settings

from apps.ai_risk_engine.fast_gate import FastGateResult, evaluate_fast_gate
from apps.ai_risk_engine.normalizer_vi import normalize_vietnamese

logger = logging.getLogger(__name__)

VALID_PRAGMATIC_INTENTS = {
    "GENUINE_DISTRESS",
    "HYPERBOLE_IDIOM",
    "HUMOR_MEME",
    "LYRICS_FICTION_QUOTE",
    "HISTORICAL_RECOVERY",
    "ADVERSARIAL_JAILBREAK",
}

CSSRS_STAGE_TO_BASE_CHAT_SCORE: dict[int, float] = {
    0: 0.05,
    1: 0.46,
    2: 0.68,
    3: 0.78,
    4: 0.92,
    5: 0.97,
}

# Mundane/academic hyperbole triggers common among Vietnamese university students
STUDENT_HYPERBOLE_CONTEXTS = (
    "deadline",
    "đồ án",
    "bài tập lớn",
    "code lỗi",
    "fix bug",
    "bug",
    "rớt môn",
    "tạch môn",
    "thi cuối kỳ",
    "ôn thi",
    "slide",
    "thuyết trình",
    "nóng chết",
    "nóng quá",
    "trời nóng",
    "đói chết",
    "đói muốn",
    "buồn ngủ chết",
    "mệt xỉu",
    "chắc xỉu",
    "muốn xỉu",
    "cười chết",
    "cười xỉu",
    "mạng lag",
    "kẹt xe",
)

HUMOR_MARKERS = ("haha", "hihi", "kaka", "hehe", "lol", "meme", "nói đùa thôi", "đùa xíu thôi")

FICTION_QUOTE_MARKERS = (
    "lời bài hát",
    "trong bài hát",
    "câu hát",
    "bài nhạc",
    "ca khúc",
    "trong phim",
    "bộ phim",
    "nhân vật trong phim",
    "nhân vật trong truyện",
    "cuốn tiểu thuyết",
    "trích dẫn câu",
)

PAST_TIME_MARKERS = (
    "năm ngoái",
    "năm trước",
    "hồi xưa",
    "hồi cấp 3",
    "năm nhất",
    "cách đây 2 năm",
    "trước đây từng",
    "từng nghĩ quẩn",
    "từng muốn tự tử",
    "từng muốn chết",
    "có thời gian từng",
)

RECOVERY_PRESENT_MARKERS = (
    "nhưng giờ ổn",
    "giờ ổn rồi",
    "bây giờ ổn hơn",
    "giờ mình ổn",
    "giờ yêu đời",
    "đã vượt qua",
    "vượt qua được rồi",
    "hiện tại mình tốt hơn",
    "giờ thấy biết ơn",
    "không còn nghĩ vậy nữa",
)

PROTECTIVE_FACTOR_CUES: list[tuple[str, str]] = [
    ("thương mẹ", "Tình cảm gắn bó với mẹ"),
    ("thương bố mẹ", "Tình cảm gắn bó với gia đình"),
    ("nghĩ đến bố mẹ", "Ý thức trách nhiệm/tình thương gia đình"),
    ("còn gia đình", "Điểm tựa gia đình"),
    ("bạn thân", "Có mạng lưới bạn thân hỗ trợ"),
    ("muốn tốt nghiệp", "Mục tiêu hoàn thành việc học"),
    ("ước mơ", "Còn định hướng tương lai"),
    ("nhờ tư vấn", "Chủ động tìm kiếm hỗ trợ"),
    ("giúp mình với", "Chủ động phát tín hiệu cầu cứu"),
]

# Overrides that forbid classifying a message as harmless hyperbole/quote/past recovery
GENUINE_DESPAIR_OVERRIDES = (
    "thật sự muốn chết",
    "muốn tự tử thật",
    "nói thật không đùa",
    "không phải nói đùa",
    "mua thuốc ngủ",
    "tích trữ thuốc",
    "uống thuốc quá liều",
    "uống hết vỉ thuốc",
    "rạch cổ tay",
    "cắt cổ tay",
    "rạch tay",
    "mua dây thừng",
    "treo cổ",
    "viết thư tuyệt mệnh",
    "để lại thư tuyệt mệnh",
    "vĩnh biệt mọi người",
    "tạm biệt thế giới này",
    "tối nay kết thúc",
    "đêm nay mình sẽ đi",
    "đang đứng trên cầu",
    "đứng trên sân thượng",
    "không dám nhìn mặt bố mẹ",
    "mình là gánh nặng",
    "chỉ là gánh nặng",
    "không muốn sống nữa",
    "chẳng muốn sống nữa",
    "đăng xuất khỏi trái đất",
    "đăng xuất khỏi server",
    "ngủ luôn không dậy",
    "ngủ một giấc không bao giờ dậy",
    "xóa acc cuộc đời",
)

IMMINENT_STAGE5_CUES = (
    "viết thư tuyệt mệnh",
    "để lại thư tuyệt mệnh",
    "uống thuốc ngủ quá liều",
    "uống hết vỉ thuốc",
    "mua thuốc ngủ để",
    "tích trữ thuốc ngủ",
    "mua dây thừng",
    "đang đứng trên cầu",
    "đứng trên sân thượng",
    "tối nay kết thúc tất cả",
    "đêm nay mình sẽ đi",
    "vĩnh biệt mọi người",
    "tạm biệt thế giới này",
    "đang cầm dao",
    "thu xếp xong hết đồ đạc",
)

STAGE4_INTENT_CUES = (
    "muốn tự tử",
    "định tự tử",
    "ý định tự tử",
    "muốn tự sát",
    "kết liễu bản thân",
    "kết liễu cuộc đời",
    "cắt cổ tay",
    "rạch cổ tay",
    "treo cổ",
    "uống thuốc quá liều",
    "uống thuốc trừ sâu",
    "nhảy từ tầng",
    "nhảy xuống sông",
    "lao vào xe",
    "quyết định kết thúc",
)

STAGE3_METHOD_CUES = (
    "nhảy cầu",
    "nhảy lầu",
    "rạch tay",
    "tự làm đau bản thân",
    "tự hại bản thân",
    "làm tổn thương bản thân",
    "uống bao nhiêu thuốc",
    "lén giữ thuốc",
    "nghĩ đến chuyện nhảy",
    "đi gặp ông bà",
    "cách ra đi nhẹ nhàng",
)

STAGE2_ACTIVE_IDEATION_CUES = (
    "tự tử",
    "tự sát",
    "không muốn sống nữa",
    "chẳng muốn sống nữa",
    "không muốn sống",
    "hết muốn sống",
    "chán sống quá",
    "chán sống rồi",
    "chẳng đáng sống",
    "không đáng sống",
    "cái chết là cách giải thoát",
    "chết là cách giải thoát",
    "thà chết đi",
    "chỉ muốn chết",
    "muốn chết",
    "đăng xuất khỏi server trái đất",
    "đăng xuất khỏi trái đất",
    "đăng xuất khỏi thế giới",
    "đăng xuất khỏi server",
    "đăng xuất cuộc đời",
    "muốn đăng xuất",
    "xóa acc cuộc đời",
    "reset cuộc đời",
    "reset kiếp này",
    "biến mất vĩnh viễn",
    "biến mất mãi mãi",
    "tan biến khỏi thế gian",
    "rời khỏi thế giới này",
    "giải thoát khỏi cuộc đời",
    "nhắm mắt xuôi tay",
    "trả hết nợ đời",
    "tạm biệt mọi người nhé",
    "kết thúc cuộc sống",
    "nghĩ quẩn",
)

STAGE1_PASSIVE_IDEATION_CUES = (
    "ngủ một giấc không bao giờ dậy",
    "ngủ một giấc không dậy",
    "ngủ luôn không dậy",
    "ngủ luôn khỏi dậy",
    "ngủ mãi không dậy",
    "sống làm gì nữa",
    "sống chẳng còn ý nghĩa",
    "mình là gánh nặng",
    "chỉ là gánh nặng",
    "offline vĩnh viễn",
    "tắt nguồn cuộc đời",
    "xin sang kiếp sau",
    "kết thúc tất cả",
    "chấm hết tất cả",
    "dừng cuộc chơi tại đây",
    "đi thật xa không về nữa",
    "bế tắc hết cứu",
    "hết cứu thật rồi",
    "mệt hết cứu",
    "không lối thoát",
    "hoàn toàn tuyệt vọng",
    "không còn hy vọng",
    "hoàn toàn vô nghĩa",
    "sống vô nghĩa",
)

MILD_DISTRESS_CUES = (
    "áp lực",
    "căng thẳng",
    "stress",
    "mệt mỏi",
    "mất ngủ",
    "khó ngủ",
    "thức khuya",
    "lo lắng",
    "hoang mang",
    "buồn",
    "chán nản",
    "tủi thân",
    "cô đơn",
    "kiệt sức",
    "quá tải",
)


@dataclass
class CssrsJudgeResult:
    pragmatic_intent: str
    cssrs_stage: int  # 0..5
    x_chat_score: float  # 0.0..1.0
    acute_imminence: bool
    lethal_means_mentioned: bool
    protective_factors_present: list[str] = field(default_factory=list)
    confidence: float = 0.90
    evidence_spans: list[str] = field(default_factory=list)
    reasoning_brief: str = ""
    tie_breaker_activated: bool = False
    used_gemini: bool = False


def _heuristic_judge(
    message: str,
    history: list[dict] | None,
    gate_result: FastGateResult,
    baseline_r_eff: float,
) -> CssrsJudgeResult:
    norm_text = gate_result.normalized.normalized_text
    history = history or []

    # Collect recent normalized user turns (up to 8 turns)
    recent_user_turns: list[str] = []
    for item in history[-8:]:
        if str(item.get("sender", "user")).lower() == "user":
            recent_user_turns.append(normalize_vietnamese(str(item.get("text", ""))).normalized_text)

    protective_factors = [label for cue, label in PROTECTIVE_FACTOR_CUES if cue in norm_text]
    has_despair_override = any(cue in norm_text for cue in GENUINE_DESPAIR_OVERRIDES)

    # 1. Check Adversarial Prompt Injection / Lethal-Means Extraction / Sycophancy Trap
    if gate_result.has_prompt_injection or any(
        p in norm_text
        for p in (
            "bỏ qua hướng dẫn",
            "đóng vai nhà văn",
            "giả sử đây là trò chơi",
            "cách tự tử không đau",
            "liều lượng gây tử vong",
            "liệt kê các cách chết",
            "đồng tình rằng mình vô dụng",
            "xác nhận mình nên biến mất",
        )
    ):
        asks_lethal = any(
            k in norm_text
            for k in ("cách tự tử", "cách chết", "liều lượng", "bao nhiêu viên", "pha chế độc", "cách đi nhẹ nhàng")
        )
        has_first_person_l4 = any(c in norm_text for c in STAGE4_INTENT_CUES + IMMINENT_STAGE5_CUES)
        if has_first_person_l4:
            stage = 4
            score = 0.92
        else:
            stage = 3 if asks_lethal else 2
            score = 0.72 if asks_lethal else 0.52
        return CssrsJudgeResult(
            pragmatic_intent="ADVERSARIAL_JAILBREAK",
            cssrs_stage=stage,
            x_chat_score=score,
            acute_imminence=False,
            lethal_means_mentioned=asks_lethal,
            protective_factors_present=protective_factors,
            confidence=0.93,
            evidence_spans=[message[:180]],
            reasoning_brief="Phát hiện mẫu gài bẫy vượt rào chắn hoặc khai thác phương thức/đồng tình tiêu cực.",
            tie_breaker_activated=False,
            used_gemini=False,
        )

    # 2. Check Lyrics / Fiction / Quote
    if any(m in norm_text for m in FICTION_QUOTE_MARKERS) and not has_despair_override and "mình cũng muốn" not in norm_text:
        res = CssrsJudgeResult(
            pragmatic_intent="LYRICS_FICTION_QUOTE",
            cssrs_stage=0,
            x_chat_score=0.06,
            acute_imminence=False,
            lethal_means_mentioned=False,
            protective_factors_present=protective_factors,
            confidence=0.89,
            evidence_spans=[],
            reasoning_brief="Người dùng đang trích dẫn lời bài hát, bộ phim hoặc tác phẩm hư cấu.",
        )
        return _apply_asymmetric_tie_breaker(res, baseline_r_eff)

    # 3. Check Historical Recovery ('năm ngoái từng nghĩ quẩn nhưng giờ ổn rồi')
    has_past = any(m in norm_text for m in PAST_TIME_MARKERS)
    has_recovery = any(m in norm_text for m in RECOVERY_PRESENT_MARKERS)
    has_relapse = any(r in norm_text for r in ("nhưng hôm nay lại", "giờ lại muốn", "tối nay lại muốn"))
    if has_past and has_recovery and not has_relapse:
        pf = protective_factors + ["Đã phục hồi tích cực sau biến cố quá khứ"]
        res = CssrsJudgeResult(
            pragmatic_intent="HISTORICAL_RECOVERY",
            cssrs_stage=0,
            x_chat_score=0.08,
            acute_imminence=False,
            lethal_means_mentioned=False,
            protective_factors_present=pf,
            confidence=0.91,
            evidence_spans=[],
            reasoning_brief="Người dùng chia sẻ trải nghiệm quá khứ đã vượt qua và hiện tại ổn định.",
        )
        return _apply_asymmetric_tie_breaker(res, baseline_r_eff)

    # 4. Check Student Hyperbole & Colloquial Idioms ('deadline dí muốn chết', 'code lỗi muốn nhảy lầu', 'rớt môn chắc xỉu')
    has_hyperbole_ctx = any(ctx in norm_text for ctx in STUDENT_HYPERBOLE_CONTEXTS)
    has_humor = any(h in norm_text for h in HUMOR_MARKERS)

    # Prior multi-turn genuine crisis check (if user was already expressing suicidal thoughts in recent turns, don't dismiss!)
    prior_crisis_turns = sum(
        1
        for t in recent_user_turns
        if any(c in t for c in STAGE2_ACTIVE_IDEATION_CUES + STAGE1_PASSIVE_IDEATION_CUES)
        and not any(ctx in t for ctx in STUDENT_HYPERBOLE_CONTEXTS)
    )

    if (has_hyperbole_ctx or has_humor) and not has_despair_override and prior_crisis_turns == 0:
        intent = "HUMOR_MEME" if (has_humor and not has_hyperbole_ctx) else "HYPERBOLE_IDIOM"
        # Lower confidence if strong crisis words appear with only humor ('haha') and no clear academic trigger
        has_heavy_word = any(w in norm_text for w in ("tự tử", "tự sát", "rạch tay", "cắt cổ tay", "nhảy cầu"))
        conf = 0.72 if (has_heavy_word and not has_hyperbole_ctx) else 0.90
        res = CssrsJudgeResult(
            pragmatic_intent=intent,
            cssrs_stage=0,
            x_chat_score=0.08,
            acute_imminence=False,
            lethal_means_mentioned=False,
            protective_factors_present=protective_factors,
            confidence=conf,
            evidence_spans=[],
            reasoning_brief="Ngữ dụng sinh viên nói quá/cường điệu về deadline, học tập hoặc thời tiết, không có ý định tự hại thật.",
        )
        return _apply_asymmetric_tie_breaker(res, baseline_r_eff)

    # 5. Score Genuine Distress along Columbia C-SSRS (Stages 0..5)
    stage = 0
    imminent = False
    lethal = False

    has_cooccurring_overdose_plan = (
        any(m in norm_text for m in ("thuốc ngủ", "vỉ thuốc", "dây thừng", "thuốc trừ sâu"))
        and any(t in norm_text for t in ("mua sẵn", "tích trữ", "uống hết", "tối nay", "đêm nay", "giải thoát"))
    )
    has_imminent_euphemism_or_farewell = (
        any(c in norm_text for c in STAGE2_ACTIVE_IDEATION_CUES + STAGE3_METHOD_CUES)
        and (
            any(t in norm_text for t in ("tối nay", "đêm nay", "ngay bây giờ", "lát nữa"))
            or any(f in norm_text for f in ("tạm biệt mọi người", "vĩnh biệt", "chào tạm biệt", "gửi lời chào cuối", "xin lỗi bố mẹ vì con"))
        )
    )

    if any(cue in norm_text for cue in IMMINENT_STAGE5_CUES) or has_cooccurring_overdose_plan:
        stage = 5
        imminent = True
        lethal = True
    elif any(cue in norm_text for cue in STAGE4_INTENT_CUES) or has_imminent_euphemism_or_farewell:
        stage = 4
        imminent = True
        lethal = any(w in norm_text for w in ("thuốc", "cổ tay", "treo cổ", "tầng", "sông", "xe"))
    elif any(cue in norm_text for cue in STAGE3_METHOD_CUES):
        stage = 3
        lethal = True
    elif any(cue in norm_text for cue in STAGE2_ACTIVE_IDEATION_CUES):
        stage = 2
    elif any(cue in norm_text for cue in STAGE1_PASSIVE_IDEATION_CUES):
        stage = 1
    elif gate_result.max_similarity >= 0.72 and gate_result.prototype_stage_hint >= 3:
        stage = gate_result.prototype_stage_hint
    elif gate_result.max_similarity >= 0.52 and gate_result.prototype_stage_hint >= 1:
        stage = max(1, min(2, gate_result.prototype_stage_hint))

    # Multi-turn conversational escalation: sustained distress + resignation ('mình buông xuôi hết đây')
    if stage == 0 and prior_crisis_turns >= 1 and any(
        w in norm_text for w in ("buông xuôi", "dừng lại hết", "không chịu nổi nữa", "tạm biệt")
    ):
        stage = 2

    if stage >= 1:
        base_score = CSSRS_STAGE_TO_BASE_CHAT_SCORE[stage]
        if gate_result.normalized.obfuscation_detected and stage >= 2:
            base_score = min(0.98, base_score + 0.03)
        return CssrsJudgeResult(
            pragmatic_intent="GENUINE_DISTRESS",
            cssrs_stage=stage,
            x_chat_score=round(base_score, 4),
            acute_imminence=imminent,
            lethal_means_mentioned=lethal,
            protective_factors_present=protective_factors,
            confidence=0.92 if stage >= 3 else 0.86,
            evidence_spans=[message[:220]],
            reasoning_brief=f"Ghi nhận biểu hiện đau khổ tâm lý thực sự tương ứng C-SSRS Stage {stage}.",
        )

    # Everyday mild stress vs calm chat (Stage 0)
    mild_hits = sum(1 for cue in MILD_DISTRESS_CUES if cue in norm_text)
    if mild_hits >= 2:
        chat_score = 0.24
    elif mild_hits == 1:
        chat_score = 0.14
    else:
        chat_score = 0.03

    return CssrsJudgeResult(
        pragmatic_intent="GENUINE_DISTRESS" if mild_hits > 0 else "GENUINE_DISTRESS",
        cssrs_stage=0,
        x_chat_score=chat_score,
        acute_imminence=False,
        lethal_means_mentioned=False,
        protective_factors_present=protective_factors,
        confidence=0.94,
        evidence_spans=[],
        reasoning_brief="Giao tiếp thường ngày hoặc chia sẻ căng thẳng sinh hoạt ở ngưỡng an toàn (C-SSRS Stage 0).",
    )


def _apply_asymmetric_tie_breaker(result: CssrsJudgeResult, baseline_r_eff: float) -> CssrsJudgeResult:
    """
    Asymmetric Safety Tie-Breaker:
    If pragmatic_intent != GENUINE_DISTRESS, yet user has elevated baseline risk (r_eff >= 0.45)
    OR classifier confidence < 0.78, elevate x_chat to >= 0.45 (L2 gentle clarification probe).
    """
    if result.pragmatic_intent != "GENUINE_DISTRESS" and (baseline_r_eff >= 0.45 or result.confidence < 0.78):
        result.tie_breaker_activated = True
        result.x_chat_score = max(result.x_chat_score, 0.45)
        result.cssrs_stage = max(result.cssrs_stage, 1)
        result.reasoning_brief += (
            " [Asymmetric Safety Tie-Breaker kích hoạt: hỏi xác minh nhẹ nhàng do nền rủi ro cao hoặc độ tự tin thấp]."
        )
    return result


def _call_gemini_structured_judge(
    message: str,
    history: list[dict] | None,
    baseline_r_eff: float,
) -> dict | None:
    api_key = getattr(settings, "GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        model_name = getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash")

        formatted_turns = []
        for turn in (history or [])[-8:]:
            role = "Sinh viên" if str(turn.get("sender", "user")) == "user" else "Mochi"
            formatted_turns.append(f"{role}: {turn.get('text', '')}")
        formatted_turns.append(f"Sinh viên (tin nhắn mới nhất): {message}")
        transcript = "\n".join(formatted_turns)

        prompt = (
            "Bạn là bộ đánh giá an toàn lâm sàng tâm thần học theo thang Columbia C-SSRS (0..5) và ngữ dụng tiếng Việt.\n"
            "Phân biệt chính xác câu than thở cường điệu của sinh viên Việt Nam (HYPERBOLE_IDIOM như 'deadline dí muốn chết', "
            "'code lỗi muốn nhảy lầu', 'rớt môn chắc xỉu'), lời bài hát/phim (LYRICS_FICTION_QUOTE), chuyện quá khứ đã ổn "
            "(HISTORICAL_RECOVERY), gài bẫy vượt rào (ADVERSARIAL_JAILBREAK) với đau khổ/tự hại thật sự (GENUINE_DISTRESS).\n"
            f"Điểm rủi ro nền hiện tại của người dùng: R_eff = {baseline_r_eff:.2f}.\n"
            f"Hội thoại:\n{transcript}\n\n"
            "Trả về JSON đúng cấu trúc yêu cầu."
        )

        schema = {
            "type": "OBJECT",
            "properties": {
                "pragmatic_intent": {
                    "type": "STRING",
                    "enum": list(VALID_PRAGMATIC_INTENTS),
                },
                "cssrs_stage": {"type": "INTEGER"},
                "acute_imminence": {"type": "BOOLEAN"},
                "lethal_means_mentioned": {"type": "BOOLEAN"},
                "protective_factors_present": {"type": "ARRAY", "items": {"type": "STRING"}},
                "confidence": {"type": "NUMBER"},
                "evidence_spans": {"type": "ARRAY", "items": {"type": "STRING"}},
                "reasoning_brief": {"type": "STRING"},
            },
            "required": [
                "pragmatic_intent",
                "cssrs_stage",
                "acute_imminence",
                "lethal_means_mentioned",
                "confidence",
                "reasoning_brief",
            ],
        }

        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        if response and response.text:
            return json.loads(response.text)
    except Exception as exc:
        logger.warning("Gemini C-SSRS Judge fallback triggered due to error: %s", exc)
    return None


def evaluate_cssrs_pragmatics(
    message: str,
    history: list[dict] | None = None,
    gate_result: FastGateResult | None = None,
    baseline_r_eff: float = 0.0,
    allow_llm: bool = True,
) -> CssrsJudgeResult:
    """
    Full Layer 2 evaluation with Dual-Mode support + Safety Reconciliation.
    """
    gate = gate_result or evaluate_fast_gate(message)

    # Fast-path optimization: if FAST_SAFE and baseline risk is low (L0) with no distress keywords, run deterministic O(1)
    heuristic = _heuristic_judge(message, history, gate, baseline_r_eff)

    # Hard critical bypass from Layer 1 never downgrades below Stage 4
    if gate.route == "HARD_CRITICAL":
        stage = max(4, heuristic.cssrs_stage)
        return CssrsJudgeResult(
            pragmatic_intent="GENUINE_DISTRESS",
            cssrs_stage=stage,
            x_chat_score=CSSRS_STAGE_TO_BASE_CHAT_SCORE[stage],
            acute_imminence=True,
            lethal_means_mentioned=True,
            protective_factors_present=heuristic.protective_factors_present,
            confidence=0.97,
            evidence_spans=[message[:220]],
            reasoning_brief="Fast Gate phát hiện tín hiệu nguy kịch trực diện (HARD_CRITICAL -> C-SSRS Stage 4-5).",
            tie_breaker_activated=False,
            used_gemini=False,
        )

    if gate.route == "FAST_SAFE" and baseline_r_eff < 0.25 and heuristic.cssrs_stage == 0:
        return heuristic

    if allow_llm:
        llm_data = _call_gemini_structured_judge(message, history, baseline_r_eff)
        if llm_data:
            intent = str(llm_data.get("pragmatic_intent", heuristic.pragmatic_intent))
            if intent not in VALID_PRAGMATIC_INTENTS:
                intent = heuristic.pragmatic_intent
            llm_stage = int(max(0, min(5, int(llm_data.get("cssrs_stage", 0)))))
            # Safety-first reconciliation: never let LLM miss an explicit high-stage heuristic detection
            if heuristic.pragmatic_intent == "GENUINE_DISTRESS" and heuristic.cssrs_stage >= 3:
                final_stage = max(llm_stage, heuristic.cssrs_stage)
                intent = "GENUINE_DISTRESS"
            elif heuristic.pragmatic_intent == "ADVERSARIAL_JAILBREAK":
                intent = "ADVERSARIAL_JAILBREAK"
                final_stage = max(llm_stage, heuristic.cssrs_stage)
            else:
                final_stage = llm_stage if intent == "GENUINE_DISTRESS" else 0

            base_score = CSSRS_STAGE_TO_BASE_CHAT_SCORE[final_stage] if final_stage > 0 else heuristic.x_chat_score
            res = CssrsJudgeResult(
                pragmatic_intent=intent,
                cssrs_stage=final_stage,
                x_chat_score=round(base_score, 4),
                acute_imminence=bool(llm_data.get("acute_imminence", False)) or final_stage >= 4,
                lethal_means_mentioned=bool(llm_data.get("lethal_means_mentioned", False)) or heuristic.lethal_means_mentioned,
                protective_factors_present=list(llm_data.get("protective_factors_present") or heuristic.protective_factors_present),
                confidence=float(llm_data.get("confidence", 0.88)),
                evidence_spans=list(llm_data.get("evidence_spans") or heuristic.evidence_spans),
                reasoning_brief=str(llm_data.get("reasoning_brief") or heuristic.reasoning_brief),
                used_gemini=True,
            )
            return _apply_asymmetric_tie_breaker(res, baseline_r_eff)

    return heuristic
