"""
Real Vietnamese Affective Sentiment & Cognitive Absolutism Analyzer (`sentiment_vi.py`)
+ Nocturnal Circadian Anomaly Helper (`00:30 - 04:30`).

Combines:
1. Multi-word Vietnamese affective lexicon (longest-phrase-first greedy scan)
2. Local negation scope ('không', 'chẳng', 'chưa', 'hết', 'đỡ', 'bớt') & intensifiers/diminishers
3. Cognitive absolutism & hopelessness markers ('vô nghĩa', 'gánh nặng', 'chấm hết', 'kiệt sức', ...)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
import unicodedata
from django.utils import timezone

# Explicit multi-word phrases containing negation words that are inherently negative
INHERENT_NEGATIVE_COMPOUNDS: dict[str, float] = {
    "không muốn sống": -4.0,
    "chẳng muốn sống": -4.0,
    "không lối thoát": -3.5,
    "không chịu nổi": -3.2,
    "không còn ý nghĩa": -3.5,
    "chẳng còn ý nghĩa": -3.5,
    "không còn hy vọng": -3.4,
    "không ai hiểu": -2.6,
    "không đủ tốt": -2.0,
    "không đủ giỏi": -2.0,
    "chưa đủ tốt": -1.6,
    "không ngủ được": -2.2,
    "mất ngủ trắng đêm": -2.8,
    "thức trắng đêm": -2.6,
    "không thể tập trung": -1.8,
    "chẳng làm được gì": -2.5,
}

# Cognitive absolutism / hopelessness markers (push mood risk up strongly)
ABSOLUTISM_MARKERS: dict[str, float] = {
    "vô nghĩa": 0.28,
    "gánh nặng": 0.34,
    "chấm hết": 0.38,
    "kiệt sức": 0.25,
    "bế tắc": 0.28,
    "vô dụng": 0.28,
    "tuyệt vọng": 0.34,
    "không lối thoát": 0.35,
    "buông xuôi": 0.28,
    "suy sụp": 0.28,
    "trống rỗng": 0.24,
    "thà chết": 0.45,
    "muốn chết": 0.42,
    "tự tử": 0.50,
    "biến mất vĩnh viễn": 0.45,
    "hoàn toàn thất bại": 0.26,
    "mệt hết cứu": 0.26,
}

# General Vietnamese affective phrases (positive > 0, negative < 0)
AFFECTIVE_LEXICON: dict[str, float] = {
    # Strong positive
    "tràn đầy năng lượng": 2.8,
    "biết ơn": 2.4,
    "hạnh phúc": 2.8,
    "yêu đời": 2.8,
    "bình yên": 2.4,
    "nhẹ nhõm": 2.2,
    "nhẹ lòng": 2.1,
    "thư thái": 2.3,
    "tuyệt vời": 2.6,
    "tự hào": 2.3,
    "tự tin": 2.1,
    "hào hứng": 2.3,
    "vui vẻ": 2.2,
    "ấm áp": 2.0,
    "lạc quan": 2.2,
    "tích cực": 2.0,
    "hy vọng": 1.9,
    "thành công": 2.1,
    # Mild positive
    "thư giãn": 1.7,
    "thoải mái": 1.7,
    "dễ chịu": 1.6,
    "trôi chảy": 1.6,
    "hoàn thành": 1.4,
    "ổn định": 1.3,
    "khá ổn": 1.2,
    "tập trung": 1.3,
    "lắng đọng": 1.4,
    "tốt hơn": 1.5,
    "vừa sức": 1.2,
    "yên tâm": 1.5,
    "vui": 1.6,
    "ổn": 0.9,
    # Mild negative
    "lo lắng": -1.5,
    "lo âu": -1.8,
    "bất an": -1.9,
    "mệt mỏi": -1.6,
    "mệt": -1.3,
    "đuối": -1.6,
    "uể oải": -1.6,
    "căng thẳng": -1.7,
    "áp lực": -1.6,
    "buồn": -1.7,
    "tủi thân": -2.0,
    "chán nản": -2.0,
    "chán": -1.4,
    "khó ngủ": -1.7,
    "mất ngủ": -2.1,
    "trằn trọc": -1.8,
    "thức khuya": -1.2,
    "hoang mang": -1.8,
    "chênh vênh": -1.9,
    "lạc lõng": -2.0,
    "cô đơn": -2.1,
    "tự ti": -1.9,
    "loay hoay": -1.3,
    "quá tải": -2.2,
    "ngột ngạt": -2.2,
    "nặng nề": -2.1,
    "nặng trĩu": -2.3,
    # Strong negative
    "hoảng loạn": -3.0,
    "đau khổ": -3.0,
    "tổn thương": -2.5,
    "khóc": -2.1,
    "sợ hãi": -2.4,
    "ám ảnh": -2.6,
    "cô độc": -2.6,
    "tồi tệ": -2.6,
    "kiệt quệ": -2.9,
    "kiệt sức": -2.7,
    "suy sụp": -3.0,
    "bế tắc": -2.9,
    "tuyệt vọng": -3.4,
    "vô nghĩa": -3.2,
    "vô dụng": -3.0,
    "gánh nặng": -3.2,
    "chấm hết": -3.5,
}

NEGATION_TOKENS = {"không", "chẳng", "chả", "chưa", "hết", "đỡ", "bớt", "khỏi"}
RELIEF_NEGATIONS = {"hết", "đỡ", "bớt", "khỏi"}
INTENSIFIER_TOKENS = {"rất", "cực", "quá", "thật", "siêu", "hoàn", "vô"}
DIMINISHER_TOKENS = {"hơi", "khá", "chút", "nhẹ", "ít"}

CLOSURE_PARADOX_CUES = (
    "thu xếp xong",
    "nhẹ lòng hẳn",
    "tặng lại",
    "gửi lại mọi người",
    "không còn vướng bận",
    "cảm ơn mọi người vì tất cả",
    "tạm biệt",
    "mọi thứ đã an bài",
)


@dataclass
class SentimentResult:
    ai_estimate_label: str
    ai_estimate_score: int  # 1..10
    ai_bar_width: int  # 10..100
    negativity_index: float  # 0.0..1.0 (for mood channel u_journal_neg)
    valence: float  # -1.0..1.0
    absolutism_hits: list[str] = field(default_factory=list)
    closure_cue_detected: bool = False


def _clean_tokens(text: str) -> tuple[str, list[str]]:
    norm = unicodedata.normalize("NFC", (text or "")).lower()
    norm = re.sub(r"[^\w\sàáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", " ", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm, norm.split() if norm else []


def analyze_vietnamese_sentiment(text: str, self_mood_score: int | None = None) -> SentimentResult:
    """
    Analyze Vietnamese journal/check-in note text using weighted phrase lexicon,
    negation scope, intensifiers, and cognitive absolutism detectors.
    """
    norm, tokens = _clean_tokens(text)
    if not tokens:
        fallback = int(max(1, min(10, self_mood_score if self_mood_score is not None else 6)))
        neg_idx = round(max(0.0, min(1.0, (10 - fallback) / 9.0 * 0.7)), 3)
        return SentimentResult(
            ai_estimate_label=_score_to_label(fallback, []),
            ai_estimate_score=fallback,
            ai_bar_width=max(10, min(100, fallback * 10)),
            negativity_index=neg_idx,
            valence=round((fallback - 5.5) / 4.5, 3),
            absolutism_hits=[],
            closure_cue_detected=False,
        )

    # Detect cognitive absolutism & closure cues
    absolutism_hits: list[str] = []
    absolutism_boost = 0.0
    for marker, penalty in ABSOLUTISM_MARKERS.items():
        if re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", norm):
            # Check not preceded immediately by relief negation like 'hết bế tắc', 'không còn vô nghĩa'
            if not re.search(rf"(?:hết|đỡ|bớt|không còn)\s+{re.escape(marker)}", norm):
                absolutism_hits.append(marker)
                absolutism_boost += penalty

    closure_detected = any(cue in norm for cue in CLOSURE_PARADOX_CUES)

    # Greedy token-window scan (4 words down to 1 word)
    combined_lexicon: dict[str, tuple[float, bool]] = {}
    for phrase, w in AFFECTIVE_LEXICON.items():
        combined_lexicon[phrase] = (w, False)
    for phrase, w in INHERENT_NEGATIVE_COMPOUNDS.items():
        combined_lexicon[phrase] = (w, True)

    n = len(tokens)
    idx = 0
    total_score = 0.0
    matched_count = 0

    while idx < n:
        matched = False
        for span_len in (4, 3, 2, 1):
            if idx + span_len > n:
                continue
            candidate = " ".join(tokens[idx : idx + span_len])
            if candidate in combined_lexicon:
                weight, inherent_neg = combined_lexicon[candidate]
                # Inspect up to 2 preceding tokens for negation / intensifier / diminisher
                prev_window = tokens[max(0, idx - 2) : idx]
                if not inherent_neg and any(tok in NEGATION_TOKENS for tok in prev_window):
                    if weight < 0 and any(tok in RELIEF_NEGATIONS or tok == "không" for tok in prev_window):
                        # e.g., 'đỡ mệt mỏi', 'hết căng thẳng', 'không buồn'
                        weight = abs(weight) * 0.45
                    elif weight > 0:
                        # e.g., 'không vui', 'chẳng bình yên', 'chưa ổn'
                        weight = -abs(weight) * 0.85
                else:
                    if any(tok in INTENSIFIER_TOKENS for tok in prev_window) or (
                        idx >= 2 and " ".join(tokens[idx - 2 : idx]) in ("cực kỳ", "vô cùng", "thực sự", "hết sức")
                    ):
                        weight *= 1.30
                    elif any(tok in DIMINISHER_TOKENS for tok in prev_window):
                        weight *= 0.72

                total_score += weight
                matched_count += 1
                idx += span_len
                matched = True
                break
        if not matched:
            idx += 1

    # Normalize valence into [-1.0, +1.0]
    denom = max(2.2, (matched_count ** 0.65) * 2.4)
    raw_valence = max(-1.0, min(1.0, total_score / denom))
    if absolutism_hits:
        raw_valence = min(raw_valence, -min(0.95, 0.35 + absolutism_boost))

    # Map valence [-1..1] -> text_score [1..10]
    text_score_float = 5.5 + raw_valence * 4.5
    if self_mood_score is not None and not absolutism_hits:
        # Blend 78% real text analysis + 22% user self-rating when text is non-empty
        text_weight = 0.82 if len(tokens) >= 6 else 0.65
        blended = text_weight * text_score_float + (1.0 - text_weight) * float(self_mood_score)
    else:
        blended = text_score_float

    if absolutism_hits:
        blended = min(blended, max(1.0, 3.2 - absolutism_boost * 2.5))

    ai_score = int(round(max(1.0, min(10.0, blended))))
    ai_bar_width = int(round(max(10.0, min(100.0, blended * 10.0))))

    # Compute negativity_index in [0.0, 1.0] for AI Risk Engine mood channel
    base_neg = max(0.0, (5.8 - blended) / 4.8) if blended < 5.8 else 0.04
    negativity_index = float(max(0.0, min(1.0, base_neg + min(0.55, absolutism_boost))))

    label = _score_to_label(ai_score, absolutism_hits)
    return SentimentResult(
        ai_estimate_label=label,
        ai_estimate_score=ai_score,
        ai_bar_width=ai_bar_width,
        negativity_index=round(negativity_index, 4),
        valence=round(raw_valence, 4),
        absolutism_hits=absolutism_hits,
        closure_cue_detected=closure_detected,
    )


def _score_to_label(score: int, absolutism_hits: list[str]) -> str:
    if absolutism_hits and score <= 2:
        return "Suy sụp & Kiệt sức"
    if absolutism_hits and score <= 4:
        return "Nặng trĩu & Bế tắc"
    if score <= 2:
        return "Căng thẳng sâu"
    if score <= 4:
        return "Nặng lòng & Lo âu"
    if score == 5:
        return "Hơi chùng xuống"
    if score == 6:
        return "Ổn định nhẹ nhàng"
    if score == 7:
        return "Tích cực nhẹ"
    if score == 8:
        return "Bình yên & Sáng rõ"
    return "Tràn đầy năng lượng"


def detect_nocturnal_window(time_str: str | None = None, timestamp: datetime | None = None) -> tuple[bool, str]:
    """
    Detect late-night circadian disturbance window (00:30 - 04:30 Asia/Ho_Chi_Minh).
    Checks explicit `time_str` (e.g. '02:15', '03:10 sáng') first if provided, else local timestamp.
    """
    if time_str:
        m = re.search(r"(\d{1,2}):(\d{2})", time_str)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2))
            lower_t = time_str.lower()
            if ("chiều" in lower_t or "tối" in lower_t or "pm" in lower_t) and hour < 12:
                hour += 12
            total_min = hour * 60 + minute
            if 30 <= total_min <= 270:
                return True, f"{hour:02d}:{minute:02d}"
            return False, f"{hour:02d}:{minute:02d}"

    dt = timestamp or timezone.now()
    local_dt = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    total_min = local_dt.hour * 60 + local_dt.minute
    hhmm = f"{local_dt.hour:02d}:{local_dt.minute:02d}"
    return (30 <= total_min <= 270), hhmm
