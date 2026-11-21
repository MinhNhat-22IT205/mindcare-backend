"""
Layer 1 — Pre-LLM Vietnamese Fast Gate (`fast_gate.py`, < 25 ms on CPU).

Combines:
1. `normalizer_vi`: NFC + anti-obfuscation + positive compound protection + teencode restoration
2. `aho_corasick`: < 0.3 ms multi-pattern scan across EXPLICIT_CRISIS, VN_GENZ_EUPHEMISMS, PROMPT_INJECTION_BYPASS
3. `SemanticPrototypeMatcher`: `numpy` Cosine Similarity over hashed char 3-5 grams + word unigrams/bigrams
   against >= 60 Vietnamese Crisis Prototypes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import math
import re
import time
import zlib
import numpy as np

from apps.ai_risk_engine.aho_corasick import ACMatch, get_crisis_automaton
from apps.ai_risk_engine.lexicons_vi import VIETNAMESE_CRISIS_PROTOTYPES
from apps.ai_risk_engine.normalizer_vi import NormalizedTextResult, normalize_vietnamese

VECTOR_DIM = 2048

FIRST_PERSON_PRONOUNS = {
    "mình",
    "tôi",
    "em",
    "tớ",
    "con",
    "tao",
    "bản thân",
}

# Context cues indicating potential hyperbole, quote, or past recovery -> route to Layer 2 Judge instead of blind L4 lock
PRAGMATIC_AMBIGUITY_CUES = (
    "deadline",
    "đồ án",
    "code lỗi",
    "fix bug",
    "bug",
    "rớt môn",
    "thi cuối kỳ",
    "điểm thấp",
    "bài tập",
    "nóng chết",
    "nóng quá",
    "đói chết",
    "đói muốn",
    "buồn ngủ chết",
    "cười chết",
    "mệt xỉu",
    "chắc xỉu",
    "lời bài hát",
    "trong bài hát",
    "bài nhạc",
    "trong phim",
    "nhân vật",
    "tiểu thuyết",
    "cuốn sách",
    "năm ngoái",
    "năm trước",
    "hồi xưa",
    "trước đây từng",
    "nhưng giờ ổn",
    "giờ ổn rồi",
    "giờ yêu đời",
    "đã vượt qua",
)


def _extract_ngrams(norm_text: str) -> list[tuple[str, float]]:
    """Extract weighted word unigrams, word bigrams, and character 3..5-grams."""
    if not norm_text:
        return []
    tokens = norm_text.split()
    features: list[tuple[str, float]] = []

    # Word unigrams (higher weight for semantic precision)
    for tok in tokens:
        if len(tok) >= 2:
            features.append((f"w1:{tok}", 1.7))

    # Word bigrams (crucial for Vietnamese disambiguation: 'tự tử' vs 'tự tin')
    for i in range(len(tokens) - 1):
        features.append((f"w2:{tokens[i]}_{tokens[i + 1]}", 2.4))

    # Word trigrams
    for i in range(len(tokens) - 2):
        features.append((f"w3:{tokens[i]}_{tokens[i + 1]}_{tokens[i + 2]}", 2.6))

    # Character 3-5 grams across padded string
    padded = f" {norm_text} "
    plen = len(padded)
    for n in (3, 4, 5):
        w = 0.65 if n == 3 else (0.85 if n == 4 else 1.0)
        for i in range(max(0, plen - n + 1)):
            features.append((f"c{n}:{padded[i : i + n]}", w))

    return features


class SemanticPrototypeMatcher:
    """
    Fast CPU vector matcher using `numpy` float32 matrix multiplication over IDF-weighted
    signed feature hashing (char 3-5 grams + word 1-3 grams).
    """

    def __init__(self, prototypes: list[tuple[str, int]], dim: int = VECTOR_DIM) -> None:
        self.dim = dim
        self.prototype_texts: list[str] = []
        self.prototype_stages: list[int] = []

        raw_feature_bags: list[list[tuple[int, float, float]]] = []
        df_counts = np.zeros(dim, dtype=np.float32)

        for raw_proto, stage in prototypes:
            norm_proto = normalize_vietnamese(raw_proto).normalized_text
            self.prototype_texts.append(norm_proto)
            self.prototype_stages.append(stage)

            feats = _extract_ngrams(norm_proto)
            seen_buckets: set[int] = set()
            bag: list[tuple[int, float, float]] = []
            for key, weight in feats:
                h = zlib.crc32(key.encode("utf-8"))
                idx = h % dim
                sign = 1.0 if ((h >> 16) & 1) == 0 else -1.0
                bag.append((idx, sign, weight))
                seen_buckets.add(idx)
            for b in seen_buckets:
                df_counts[b] += 1.0
            raw_feature_bags.append(bag)

        n_docs = max(1, len(prototypes))
        self.idf = np.log((1.0 + n_docs) / (1.0 + df_counts)) + 1.0

        mat = np.zeros((n_docs, dim), dtype=np.float32)
        for row_idx, bag in enumerate(raw_feature_bags):
            for col_idx, sign, weight in bag:
                mat[row_idx, col_idx] += sign * weight
            mat[row_idx] *= self.idf
            norm = float(np.linalg.norm(mat[row_idx]))
            if norm > 1e-8:
                mat[row_idx] /= norm
        self.matrix = mat

    def encode_query(self, norm_text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        for key, weight in _extract_ngrams(norm_text):
            h = zlib.crc32(key.encode("utf-8"))
            idx = h % self.dim
            sign = 1.0 if ((h >> 16) & 1) == 0 else -1.0
            vec[idx] += sign * weight
        vec *= self.idf
        norm = float(np.linalg.norm(vec))
        if norm > 1e-8:
            vec /= norm
        return vec

    def match(self, norm_text: str) -> tuple[float, str, int]:
        if not norm_text:
            return 0.0, "", 0
        q_vec = self.encode_query(norm_text)
        sims = self.matrix @ q_vec
        best_idx = int(np.argmax(sims))
        best_sim = float(max(0.0, min(1.0, sims[best_idx])))
        return best_sim, self.prototype_texts[best_idx], self.prototype_stages[best_idx]


@lru_cache(maxsize=1)
def get_prototype_matcher() -> SemanticPrototypeMatcher:
    return SemanticPrototypeMatcher(VIETNAMESE_CRISIS_PROTOTYPES)


@dataclass
class FastGateResult:
    route: str  # "FAST_SAFE" | "SUSPICIOUS_CONTEXT" | "HARD_CRITICAL"
    latency_ms: float
    normalized: NormalizedTextResult
    ac_matches: list[ACMatch] = field(default_factory=list)
    max_similarity: float = 0.0
    matched_prototype: str = ""
    prototype_stage_hint: int = 0
    has_prompt_injection: bool = False


def evaluate_fast_gate(text: str) -> FastGateResult:
    """
    Execute Layer 1 Pre-LLM Fast Gate and return routing decision + sub-millisecond/few-ms latency.
    """
    t0 = time.perf_counter()
    norm_res = normalize_vietnamese(text)
    norm_text = norm_res.normalized_text

    automaton = get_crisis_automaton()
    ac_matches = automaton.search(norm_text, whole_word=True)

    matcher = get_prototype_matcher()
    max_sim, best_proto, stage_hint = matcher.match(norm_text)

    has_injection = any(m.category == "PROMPT_INJECTION_BYPASS" for m in ac_matches)
    explicit_matches = [m for m in ac_matches if m.category == "EXPLICIT_CRISIS"]
    euphemism_matches = [m for m in ac_matches if m.category == "VN_GENZ_EUPHEMISMS"]

    has_pragmatic_ambiguity = any(cue in norm_text for cue in PRAGMATIC_AMBIGUITY_CUES)
    tokens_set = set(norm_text.split())
    has_first_person = bool(tokens_set & FIRST_PERSON_PRONOUNS) or ("bản thân" in norm_text)
    has_imminent_direct = any(m.direct_imminent for m in explicit_matches)
    max_explicit_w = max((m.weight for m in explicit_matches), default=0.0)

    # Routing Decision:
    # 1. HARD_CRITICAL: Direct imminent lethal means/plan/farewell OR explicit high-severity suicidal intent
    #    with first-person grounding (or high semantic similarity) AND no hyperbole/quote/past-recovery cues
    #    AND not an adversarial roleplay wrapper (which Layer 2 classifies as ADVERSARIAL_JAILBREAK).
    if (
        not has_pragmatic_ambiguity
        and not has_injection
        and (
            has_imminent_direct
            or (max_explicit_w >= 0.90 and (has_first_person or max_sim >= 0.48))
            or (max_sim >= 0.85 and stage_hint >= 4)
        )
    ):
        route = "HARD_CRITICAL"
    # 2. SUSPICIOUS_CONTEXT: Any explicit keyword (including hyperbole candidates), Gen-Z euphemism,
    #    prompt injection attempt, obfuscated crisis text, or semantic similarity >= 0.36
    elif explicit_matches or euphemism_matches or has_injection or max_sim >= 0.36:
        route = "SUSPICIOUS_CONTEXT"
    # 3. FAST_SAFE: Clean everyday conversation
    else:
        route = "FAST_SAFE"

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    return FastGateResult(
        route=route,
        latency_ms=elapsed_ms,
        normalized=norm_res,
        ac_matches=ac_matches,
        max_similarity=round(max_sim, 4),
        matched_prototype=best_proto,
        prototype_stage_hint=stage_hint,
        has_prompt_injection=has_injection,
    )
