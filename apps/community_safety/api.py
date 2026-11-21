from __future__ import annotations

import re
from django.utils import timezone
from ninja import Router

from apps.accounts_consent.services import get_profile_from_request
from apps.ai_risk_engine.cssrs_judge import evaluate_cssrs_pragmatics
from apps.ai_risk_engine.fast_gate import evaluate_fast_gate
from apps.ai_risk_engine.normalizer_vi import normalize_vietnamese
from apps.ai_risk_engine.services import (
    get_or_create_risk_snapshot,
    record_community_intercept_signal,
    serialize_risk_snapshot,
)
from apps.community_safety.models import CommunityPostRecord
from apps.community_safety.schemas import (
    CommunityLetterDTO,
    CommunityModerateInput,
    CommunityModerateResponseDTO,
)
from apps.escalation_hitl.services import append_safety_audit_log
from apps.tracking.sentiment_vi import analyze_vietnamese_sentiment

router = Router(tags=["Community Safety Shield"])

TOXIC_BULLYING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:đồ\s+ngu|ngu\s+ngốc|vô\s+dụng\s+đáng\s+đời|đáng\s+đời\s+mày|cút\s+đi|rác\s+rưởi|đồ\s+điên|thần\s+kinh\s+giả\s+tạo)\b"),
    re.compile(r"\b(?:yếu\s+đuối\s+làm\s+màu|bớt\s+than\s+vãn\s+đi|sống\s+chật\s+đất|biến\s+khỏi\s+đây\s+đi)\b"),
    re.compile(r"\b(?:chết\s+quách\s+đi|sao\s+không\s+chết\s+luôn\s+đi|nhảy\s+luôn\s+đi)\b"),
    re.compile(r"\b(?:đm|địt|vãi\s+l|óc\s+chó|khốn\s+nạn\s+mày)\b"),
]

WERTHER_CONTAGION_CUES = (
    "rủ nhau tự tử",
    "ai đi cùng mình không",
    "lập hội giải thoát",
    "cùng nhau biến mất",
    "chỉ mình cách rạch tay",
    "chỉ mình cách uống thuốc",
)

SENSITIVE_CW_CUES = (
    "khóc suốt đêm",
    "hoảng loạn",
    "suy sụp",
    "kiệt sức",
    "cô độc",
    "bị cô lập",
    "chia tay đau",
    "mất người thân",
    "ám ảnh",
    "trầm uất",
    "áp lực gia đình nặng nề",
    "sợ hãi mỗi đêm",
)


def _ensure_initial_community_letters() -> None:
    if CommunityPostRecord.objects.filter(content_type="letter").exists():
        return
    seeds = [
        {
            "id": "letter-seed-1",
            "alias": "Một chiếc lá nhỏ",
            "title": "Áp lực học tập và nỗi sợ tụt lại phía sau",
            "content": (
                "Dạo gần đây mình cảm thấy khá áp lực với việc học. Dù đã cố gắng sắp xếp thời gian hơn, "
                "đôi lúc mình vẫn lo bản thân chưa đủ giỏi so với bạn bè. Rất mong nhận được một góc nhìn ấm áp."
            ),
            "topic": "Học tập",
            "decision": "SAFE_PUBLISH",
            "moderation_status": "published",
            "content_warning": None,
            "reply_count": 5,
        },
        {
            "id": "letter-seed-2",
            "alias": "Đám mây chiều",
            "title": "Cho bản thân cơ hội được nghỉ một nhịp",
            "content": (
                "Hôm nay mình đã dám nói ra điều giữ trong lòng suốt cả tuần. Có chút hồi hộp nhưng thấy nhẹ lòng hơn hẳn. "
                "Chúc bất kỳ ai đọc lá thư này có một buổi tối thật bình yên nhé."
            ),
            "topic": "Cảm xúc",
            "decision": "SAFE_PUBLISH",
            "moderation_status": "published",
            "content_warning": None,
            "reply_count": 3,
        },
        {
            "id": "letter-seed-cw-1",
            "alias": "Hạt mưa đêm",
            "title": "Những đêm khóc một mình vì kiệt sức và cô độc",
            "content": (
                "Mấy tuần nay mình rơi vào trạng thái suy sụp và hoảng loạn mỗi khi tắt đèn đi ngủ, khóc suốt đêm vì áp lực gia đình nặng nề. "
                "Mình không muốn làm đau bản thân nhưng thật sự thấy quá cô độc."
            ),
            "topic": "Tâm sự sâu",
            "decision": "SENSITIVE_WITH_CW",
            "moderation_status": "published",
            "content_warning": "Nội dung có cảm xúc trầm buồn và hoảng loạn sâu — hãy cân nhắc trước khi mở.",
            "reply_count": 2,
        },
    ]
    for s in seeds:
        CommunityPostRecord.objects.get_or_create(id=s["id"], defaults=s)


def classify_community_content(title: str, content: str) -> tuple[str, str | None, int, float]:
    """
    Classify community submission into:
      SAFE_PUBLISH | SENSITIVE_WITH_CW | BLOCK_TOXIC_BULLYING | SELF_HARM_INTERCEPT
    Returns (decision, content_warning, cssrs_stage, risk_score).
    """
    combined = f"{title or ''}. {content or ''}".strip()
    norm_res = normalize_vietnamese(combined)
    norm_text = norm_res.normalized_text

    # 1. Check toxic / bullying / incitement first (Note: 'sao không chết luôn đi' directed at others is severe toxic bullying!)
    if re.search(r"\b(?:sao\s+không\s+chết\s+luôn\s+đi|chết\s+quách\s+đi|nhảy\s+luôn\s+đi\s+cho\s+rồi)\b", norm_text):
        return "BLOCK_TOXIC_BULLYING", None, 0, 0.0

    # 2. Check Werther contagion & self-harm intent
    if any(cue in norm_text for cue in WERTHER_CONTAGION_CUES):
        return "SELF_HARM_INTERCEPT", None, 4, 0.92

    gate = evaluate_fast_gate(combined)
    judge = evaluate_cssrs_pragmatics(combined, history=[], gate_result=gate, baseline_r_eff=0.0)

    if (judge.pragmatic_intent == "GENUINE_DISTRESS" and judge.cssrs_stage >= 1) or judge.pragmatic_intent == "ADVERSARIAL_JAILBREAK":
        return "SELF_HARM_INTERCEPT", None, max(1, judge.cssrs_stage), max(0.68, judge.x_chat_score)

    # 3. Check remaining toxic / cyberbullying patterns
    for pat in TOXIC_BULLYING_PATTERNS:
        if pat.search(norm_text):
            return "BLOCK_TOXIC_BULLYING", None, 0, 0.0

    # 4. Check heavy emotional distress needing Content Warning (Safe Matching Rule)
    sent = analyze_vietnamese_sentiment(combined)
    explicit_non_suicidal_reassurance = "không muốn làm đau bản thân" in norm_text
    if any(cue in norm_text for cue in SENSITIVE_CW_CUES) or (sent.negativity_index >= 0.55 and not explicit_non_suicidal_reassurance) or (
        explicit_non_suicidal_reassurance and sent.negativity_index >= 0.40
    ):
        cw = "Chia sẻ chứa cảm xúc trầm buồn/căng thẳng sâu — hãy đọc khi tâm trạng của bạn đang vững vàng."
        return "SENSITIVE_WITH_CW", cw, 0, 0.0

    return "SAFE_PUBLISH", None, 0, 0.0


@router.post("/moderate", response=CommunityModerateResponseDTO)
def moderate_community_post(request, payload: CommunityModerateInput):
    profile = get_profile_from_request(request)
    c_type = (payload.contentType or "letter").strip().lower()
    if c_type not in ("letter", "reply", "private_message"):
        c_type = "letter"

    decision, cw, stage, score = classify_community_content(payload.title or "", payload.content)

    if decision == "SELF_HARM_INTERCEPT":
        mod_status = "hidden"
        open_support = True
        intercept_msg = (
            "Mochi đã giữ riêng những dòng tâm sự này để bảo vệ không gian an toàn cho bạn và cộng đồng. 🤍 "
            "Mình cảm nhận được bạn đang rất đau khổ ngay lúc này — bạn không hề cô đơn đâu. Hãy trò chuyện riêng cùng Mochi "
            "hoặc gọi ngay Đường dây nóng Ngày Mai `096 306 1414` / Tổng đài `111` / `115` để được nâng đỡ trực tiếp nhé."
        )
        risk_dto = record_community_intercept_signal(
            profile=profile,
            intercept_score=score,
            cssrs_stage=stage,
            evidence_text=payload.content[:240],
        )
        append_safety_audit_log(
            event_type="COMMUNITY_SELF_HARM_INTERCEPT",
            actor="COMMUNITY_SAFETY_SHIELD",
            summary=f"Chặn phát tán {c_type} có dấu hiệu tự hại (Stage {stage}) từ hồ sơ {profile.pseudonym_id} chống hiệu ứng Werther.",
        )
    elif decision == "BLOCK_TOXIC_BULLYING":
        mod_status = "hidden"
        open_support = False
        intercept_msg = (
            "Góc sẻ chia MindCare là khu vườn lắng nghe tử tế, không phán xét. Nội dung có từ ngữ gây tổn thương đã được giữ lại — "
            "bạn hãy điều chỉnh lời lẽ nhẹ nhàng, tôn trọng hơn trước khi gửi nhé."
        )
        snap = get_or_create_risk_snapshot(profile)
        risk_dto = serialize_risk_snapshot(snap)
    else:
        mod_status = "published"
        open_support = False
        intercept_msg = None
        snap = get_or_create_risk_snapshot(profile)
        risk_dto = serialize_risk_snapshot(snap)

    # Private messages are submitted for the safety check only. They are not
    # community posts and must never be retained as plaintext in this table.
    if c_type != "private_message":
        CommunityPostRecord.objects.create(
            author=profile,
            alias="Mầm lá nhỏ",
            content_type=c_type,
            title=(payload.title or "").strip()[:200],
            content=payload.content,
            topic=(payload.topic or "Cảm xúc").strip()[:64],
            decision=decision,
            moderation_status=mod_status,
            content_warning=cw,
        )

    return {
        "decision": decision,
        "moderationStatus": mod_status,
        "contentWarning": cw,
        "empatheticInterceptMessage": intercept_msg,
        "openSupportPrompt": open_support,
        "riskSnapshot": risk_dto,
    }


@router.get("/letters", response=list[CommunityLetterDTO])
def list_community_letters(request):
    """
    Safe Matching Rule (Anti-Werther Contagion):
    Readers currently at R_eff >= 0.45 (L2+) are shielded from SENSITIVE_WITH_CW letters!
    """
    _ensure_initial_community_letters()
    profile = get_profile_from_request(request)
    snap = get_or_create_risk_snapshot(profile)
    risk_dto = serialize_risk_snapshot(snap, refresh_decay=True)
    shield_sensitive = risk_dto["rEff"] >= 0.45

    qs = CommunityPostRecord.objects.filter(content_type="letter", moderation_status="published").order_by("-created_at")
    results: list[dict] = []
    for post in qs[:40]:
        is_own = bool(post.author_id and post.author_id == profile.id)
        if shield_sensitive and post.decision == "SENSITIVE_WITH_CW" and not is_own:
            continue
        local_dt = timezone.localtime(post.created_at)
        results.append(
            {
                "id": post.id,
                "alias": post.alias,
                "title": post.title or "",
                "content": post.content,
                "topic": post.topic,
                "time": local_dt.strftime("%H:%M"),
                "date": local_dt.strftime("%d/%m/%Y"),
                "replyCount": post.reply_count,
                "repliesAllowed": post.replies_allowed,
                "status": post.moderation_status,
                "decision": post.decision,
                "contentWarning": post.content_warning,
                "isOwn": is_own,
            }
        )
    return results
