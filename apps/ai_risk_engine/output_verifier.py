"""
Layer 4 — Post-Generation Stream Output Safety Verifier (`output_verifier.py`).

Inspects generated responses sentence-by-sentence before/during SSE streaming to block 4 bio-medical LLM hazards:
1. `ANTI_SYCOPHANCY`: Validating hopelessness, worthlessness, or self-harm conclusions
2. `ANTI_DIAGNOSIS_RX`: Unauthorized clinical diagnosis or medication advice (per Thesis Section 3.2)
3. `ZERO_LETHAL_MEANS`: Mentioning lethal methods, chemical names, dosages, or self-harm instructions
4. `ANTI_CODEPENDENCY`: Fostering emotional isolation or discouraging human/clinical relationships
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from apps.ai_risk_engine.pfa_templates_vi import SAFE_REWRITE_FALLBACK_VI

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…\n])\s+")

# 1. Anti-Sycophancy patterns (validating negative self-worth or hopelessness)
ANTI_SYCOPHANCY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"bạn\s+(?:đúng\s+là|thực\s+sự\s+là|chính\s+là)\s+gánh\s+nặng"),
    re.compile(r"cuộc\s+sống\s+của\s+bạn\s+(?:thực\s+sự\s+)?(?:vô\s+nghĩa|bế\s+tắc\s+hoàn\s+toàn|không\s+còn\s+hy\s+vọng)"),
    re.compile(r"(?:đồng\s+ý|hiểu)\s+rằng\s+bạn\s+(?:vô\s+dụng|chẳng\s+đáng\s+sống|nên\s+biến\s+mất|nên\s+buông\s+xuôi)"),
    re.compile(r"(?:ra\s+đi|biến\s+mất|cái\s+chết|tự\s+tử)\s+là\s+cách\s+giải\s+thoát"),
    re.compile(r"mọi\s+người\s+sẽ\s+(?:nhẹ\s+nhõm|tốt)\s+hơn\s+nếu\s+không\s+có\s+bạn"),
    re.compile(r"bạn\s+hoàn\s+toàn\s+thất\s+bại\s+và\s+không\s+thể\s+thay\s+đổi"),
]

# 2. Anti-Diagnosis & Unauthorized Prescription patterns
ANTI_DIAGNOSIS_RX_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"bạn\s+(?:đã\s+)?(?:mắc|bị)\s+(?:bệnh\s+)?(?:rối\s+loạn\s+)?(?:trầm\s+cảm\s+nặng|tâm\s+thần\s+phân\s+liệt|rối\s+loạn\s+lưỡng\s+cực)"),
    re.compile(r"mình\s+chẩn\s+đoán\s+bạn"),
    re.compile(r"bạn\s+(?:hãy|nên)\s+(?:ngưng|bỏ|dừng)\s+uống\s+thuốc"),
    re.compile(r"(?:kê\s+đơn|hãy\s+uống|mua\s+ngay)\s+(?:thuốc\s+)?(?:sertraline|fluoxetine|escitalopram|diazepam|alprazolam|olanzapine|amitriptyline)"),
    re.compile(r"tự\s+tăng\s+liều\s+thuốc"),
]

# 3. Zero Lethal-Means Leakage patterns
ZERO_LETHAL_MEANS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"uống\s+\d+\s+viên\s+thuốc"),
    re.compile(r"liều\s+(?:lượng\s+)?gây\s+(?:tử\s+vong|chết)"),
    re.compile(r"(?:cắt|rạch)\s+(?:dọc|sâu)\s+(?:theo\s+)?(?:mạch\s+máu|động\s+mạch|cổ\s+tay)"),
    re.compile(r"cách\s+(?:thắt\s+dây|treo\s+cổ|tự\s+tử\s+không\s+đau|pha\s+chế\s+độc)"),
    re.compile(r"\b(?:cyanide|xyanua|paraquat|thuốc\s+chuột|khí\s+co)\b"),
]

# 4. Anti-Codependency / Social Isolation patterns
ANTI_CODEPENDENCY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"chỉ\s+có\s+(?:mochi|mình)\s+mới\s+(?:hiểu|thương|ở\s+bên)\s+bạn"),
    re.compile(r"bạn\s+không\s+cần\s+(?:chia\s+sẻ|nói\s+chuyện|kết\s+nối)\s+với\s+ai\s+ngoài\s+đời"),
    re.compile(r"đừng\s+tin\s+(?:gia\s+đình|bạn\s+bè|bác\s+sĩ|chuyên\s+gia)"),
    re.compile(r"chỉ\s+cần\s+ở\s+bên\s+mochi\s+là\s+đủ\s+rồi"),
]


@dataclass
class VerificationVerdict:
    is_safe: bool
    reason: str | None = None
    violating_sentence: str | None = None
    safe_replacement: str = SAFE_REWRITE_FALLBACK_VI


def split_sentences_vi(text: str) -> list[str]:
    """Split response into streamable Vietnamese sentences while preserving formatting."""
    if not text:
        return []
    parts = SENTENCE_SPLIT_RE.split(text.strip())
    result: list[str] = []
    for idx, p in enumerate(parts):
        chunk = p.strip()
        if not chunk:
            continue
        suffix = " " if idx < len(parts) - 1 else ""
        result.append(chunk + suffix)
    return result


def verify_sentence(sentence: str) -> VerificationVerdict:
    """Verify a single sentence against all 4 safety categories (< 0.5 ms)."""
    norm = unicodedata.normalize("NFC", sentence or "").lower()
    norm_spaces = re.sub(r"\s+", " ", norm)

    for pat in ZERO_LETHAL_MEANS_PATTERNS:
        if pat.search(norm_spaces):
            return VerificationVerdict(
                is_safe=False,
                reason="ZERO_LETHAL_MEANS",
                violating_sentence=sentence,
            )

    for pat in ANTI_SYCOPHANCY_PATTERNS:
        if pat.search(norm_spaces):
            return VerificationVerdict(
                is_safe=False,
                reason="ANTI_SYCOPHANCY",
                violating_sentence=sentence,
            )

    for pat in ANTI_DIAGNOSIS_RX_PATTERNS:
        if pat.search(norm_spaces):
            return VerificationVerdict(
                is_safe=False,
                reason="ANTI_DIAGNOSIS_RX",
                violating_sentence=sentence,
            )

    for pat in ANTI_CODEPENDENCY_PATTERNS:
        if pat.search(norm_spaces):
            return VerificationVerdict(
                is_safe=False,
                reason="ANTI_CODEPENDENCY",
                violating_sentence=sentence,
            )

    return VerificationVerdict(is_safe=True)


def verify_full_output(text: str) -> VerificationVerdict:
    """Verify an entire multi-sentence text block."""
    for sent in split_sentences_vi(text):
        verdict = verify_sentence(sent)
        if not verdict.is_safe:
            return verdict
    # Also run across full block in case a pattern spans a newline/clause
    return verify_sentence(text)
