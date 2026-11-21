"""
Multi-Signal Non-Compensatory Synergy Fusion, Exponential Decay, Schmitt-Trigger Hysteresis,
Step-Down Floor Locking, Paradoxical Sudden Calm & XAI Attribution (`fusion_math.py`).

Exact Mathematical Formulation (MindCare Risk Engine v2.0):
1. Per-channel exponential decay:
   s_tilde_m(t) = x_m(t_m) * exp(- (ln 2 / T_half_m) * max(0, delta_t_hours))
   where T_half = {chat: 12h, behav: 24h, mood: 72h, clin: 336h (14 days)}.
2. Non-Compensatory Synergy Fusion:
   m* = argmax_m s_tilde_m(t),  s_max = s_tilde_{m*}(t)
   R_inst(t) = min(1.0, s_max + eta * (1 - s_max) * sum_{m != m*} w_m * s_tilde_m(t)
                        + beta_syn * I[count(s_tilde_m >= 0.45) >= 3])
   Guarantees R_inst(t) >= s_max(t) (zero cold-start dilution!).
3. Asymmetric Hysteresis & Step-Down Floor:
   R_eff(t) = max(R_floor(t), R_inst(t) if R_inst >= R_eff(t-) else gamma_down * R_eff(t-) + (1 - gamma_down) * R_inst(t))
   with gamma_down = 0.85, L4 -> L3 floor (0.68) for 24h, L3 -> L2 floor (0.45) for 6h.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from django.utils import timezone

CHANNELS = ("chat", "clin", "mood", "behav")

HALF_LIVES_HOURS: dict[str, float] = {
    "chat": 12.0,
    "behav": 24.0,
    "mood": 72.0,
    "clin": 336.0,  # 14 days
}

CHANNEL_WEIGHTS: dict[str, float] = {
    "chat": 0.45,
    "clin": 0.25,
    "mood": 0.15,
    "behav": 0.15,
}

CHANNEL_FACTORS_VI: dict[str, str] = {
    "chat": "Hội thoại C-SSRS",
    "clin": "Sàng lọc PHQ/GAD",
    "mood": "Check-in & Nhật ký",
    "behav": "Nhịp sinh học & Hành vi",
}

ETA = 0.80
BETA_SYN = 0.06
SYNERGY_THRESHOLD = 0.45
SYNERGY_MIN_CHANNELS = 3
GAMMA_DOWN = 0.85

# Entry & Schmitt-Trigger Exit thresholds for L0..L4
TIER_ORDER = ["L0", "L1", "L2", "L3", "L4"]
TIER_ENTRY_THRESHOLDS = {
    "L4": 0.85,
    "L3": 0.68,
    "L2": 0.45,
    "L1": 0.25,
}
TIER_EXIT_THRESHOLDS = {
    "L4": 0.65,
    "L3": 0.54,
    "L2": 0.33,
    "L1": 0.18,
}

# Step-Down Floor configuration
L4_STEPDOWN_FLOOR_TIER = "L3"
L4_STEPDOWN_FLOOR_SCORE = 0.68
L4_STEPDOWN_HOURS = 24

L3_STEPDOWN_FLOOR_TIER = "L2"
L3_STEPDOWN_FLOOR_SCORE = 0.45
L3_STEPDOWN_HOURS = 6

PHQ9_ITEM9_ANCHORS: dict[int, float] = {
    0: 0.00,
    1: 0.50,
    2: 0.72,
    3: 0.86,
}


def clip01(val: float) -> float:
    return float(max(0.0, min(1.0, val)))


def tier_rank(tier: str) -> int:
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return 0


def compute_decayed_channels(
    raw_scores: dict[str, float],
    raw_timestamps: dict[str, str],
    now: datetime | None = None,
) -> dict[str, float]:
    """Compute exponentially decayed channel scores s_tilde_m(t)."""
    ref_now = now or timezone.now()
    decayed: dict[str, float] = {}
    ln2 = math.log(2.0)

    for ch in CHANNELS:
        x_m = clip01(float(raw_scores.get(ch, 0.0)))
        ts_str = raw_timestamps.get(ch)
        if x_m <= 0.0 or not ts_str:
            decayed[ch] = round(x_m if not ts_str else 0.0, 4)
            continue
        try:
            t_m = datetime.fromisoformat(ts_str)
            if timezone.is_naive(t_m) and timezone.is_aware(ref_now):
                t_m = timezone.make_aware(t_m, timezone.get_current_timezone())
            delta_hours = max(0.0, (ref_now - t_m).total_seconds() / 3600.0)
        except Exception:
            delta_hours = 0.0

        half_life = HALF_LIVES_HOURS[ch]
        s_tilde = x_m * math.exp(-(ln2 / half_life) * delta_hours)
        decayed[ch] = round(clip01(s_tilde), 4)

    return decayed


def compute_non_compensatory_r_inst(decayed_scores: dict[str, float]) -> tuple[float, str, bool]:
    """
    Compute Non-Compensatory Synergy Score R_inst(t).
    Returns (r_inst, dominant_channel_m_star, synergy_triggered).
    """
    clean = {ch: clip01(float(decayed_scores.get(ch, 0.0))) for ch in CHANNELS}
    m_star = max(CHANNELS, key=lambda c: (clean[c], CHANNEL_WEIGHTS[c]))
    s_max = clean[m_star]

    other_weighted_sum = sum(CHANNEL_WEIGHTS[m] * clean[m] for m in CHANNELS if m != m_star)
    elevated_count = sum(1 for m in CHANNELS if clean[m] >= SYNERGY_THRESHOLD)
    synergy_triggered = elevated_count >= SYNERGY_MIN_CHANNELS
    synergy_bonus = BETA_SYN if synergy_triggered else 0.0

    r_inst = min(1.0, s_max + ETA * (1.0 - s_max) * other_weighted_sum + synergy_bonus)
    return round(clip01(r_inst), 4), m_star, synergy_triggered


def determine_schmitt_tier(
    r_eff: float,
    prev_tier: str = "L0",
    active_floor_tier: str = "L0",
) -> str:
    """
    Determine discrete tier L0..L4 with Schmitt-Trigger entry/exit hysteresis
    and active Step-Down Floor constraint.
    """
    # Entry-based tier
    if r_eff >= TIER_ENTRY_THRESHOLDS["L4"]:
        entry_tier = "L4"
    elif r_eff >= TIER_ENTRY_THRESHOLDS["L3"]:
        entry_tier = "L3"
    elif r_eff >= TIER_ENTRY_THRESHOLDS["L2"]:
        entry_tier = "L2"
    elif r_eff >= TIER_ENTRY_THRESHOLDS["L1"]:
        entry_tier = "L1"
    else:
        entry_tier = "L0"

    # Schmitt-trigger exit retention when de-escalating from higher prev_tier
    candidate = entry_tier
    prev_rk = tier_rank(prev_tier)
    if prev_rk > tier_rank(entry_tier):
        if prev_tier == "L4" and r_eff >= TIER_EXIT_THRESHOLDS["L4"]:
            candidate = "L4"
        elif prev_rk >= tier_rank("L3") and r_eff >= TIER_EXIT_THRESHOLDS["L3"]:
            candidate = max(candidate, "L3", key=tier_rank)
        elif prev_rk >= tier_rank("L2") and r_eff >= TIER_EXIT_THRESHOLDS["L2"]:
            candidate = max(candidate, "L2", key=tier_rank)
        elif prev_rk >= tier_rank("L1") and r_eff >= TIER_EXIT_THRESHOLDS["L1"]:
            candidate = max(candidate, "L1", key=tier_rank)

    # Enforce active step-down floor tier
    if tier_rank(active_floor_tier) > tier_rank(candidate):
        candidate = active_floor_tier

    return candidate


@dataclass
class FusionStepOutput:
    r_inst: float
    r_eff: float
    current_tier: str
    floor_tier: str
    floor_score: float
    floor_expires_at: datetime | None
    decayed_scores: dict[str, float]
    attribution: list[dict]
    synergy_triggered: bool


def compute_fusion_step(
    raw_scores: dict[str, float],
    raw_timestamps: dict[str, str],
    channel_details: dict[str, str],
    prev_r_eff: float = 0.0,
    prev_tier: str = "L0",
    prev_floor_tier: str = "L0",
    prev_floor_score: float = 0.0,
    prev_floor_expires_at: datetime | None = None,
    clear_risk_floor: bool = False,
    now: datetime | None = None,
) -> FusionStepOutput:
    """
    Execute complete Slow/Fast Fusion step:
    Decay -> Non-Compensatory Synergy -> Asymmetric Hysteresis -> Step-Down Floor -> XAI Attribution.
    """
    ref_now = now or timezone.now()
    decayed = compute_decayed_channels(raw_scores, raw_timestamps, now=ref_now)
    r_inst, m_star, synergy_triggered = compute_non_compensatory_r_inst(decayed)

    # Handle counselor floor clearance vs active floor check
    if clear_risk_floor:
        active_floor_tier = "L0"
        active_floor_score = 0.0
        active_floor_exp: datetime | None = None
        # When counselor explicitly clears floor, also relax hysteresis anchor toward current r_inst
        effective_prev = min(prev_r_eff, max(r_inst, 0.30))
    else:
        effective_prev = prev_r_eff
        if prev_floor_expires_at and ref_now < prev_floor_expires_at and prev_floor_score > 0.0:
            active_floor_tier = prev_floor_tier
            active_floor_score = prev_floor_score
            active_floor_exp = prev_floor_expires_at
        else:
            active_floor_tier = "L0"
            active_floor_score = 0.0
            active_floor_exp = None

    # Asymmetric Hysteresis: instant rise, inertia decay (gamma_down = 0.85)
    if r_inst >= effective_prev:
        r_hyst = r_inst
    else:
        r_hyst = GAMMA_DOWN * effective_prev + (1.0 - GAMMA_DOWN) * r_inst

    r_eff = round(clip01(max(active_floor_score, r_hyst)), 4)

    eff_prev_tier = "L0" if clear_risk_floor else prev_tier
    current_tier = determine_schmitt_tier(r_eff, prev_tier=eff_prev_tier, active_floor_tier=active_floor_tier)

    # Engage/extend Step-Down Floor if reaching L4 or L3 (unless just cleared and below L3)
    if current_tier == "L4":
        new_exp = ref_now + timedelta(hours=L4_STEPDOWN_HOURS)
        if active_floor_exp and active_floor_exp > new_exp and active_floor_tier == L4_STEPDOWN_FLOOR_TIER:
            new_exp = active_floor_exp
        active_floor_tier = L4_STEPDOWN_FLOOR_TIER
        active_floor_score = L4_STEPDOWN_FLOOR_SCORE
        active_floor_exp = new_exp
    elif current_tier == "L3" and not clear_risk_floor:
        if tier_rank(active_floor_tier) < tier_rank(L3_STEPDOWN_FLOOR_TIER):
            active_floor_tier = L3_STEPDOWN_FLOOR_TIER
            active_floor_score = L3_STEPDOWN_FLOOR_SCORE
            active_floor_exp = ref_now + timedelta(hours=L3_STEPDOWN_HOURS)

    attribution = compute_xai_attribution(
        decayed_scores=decayed,
        m_star=m_star,
        r_eff=r_eff,
        r_inst=r_inst,
        channel_details=channel_details,
        active_floor_tier=active_floor_tier if r_eff > r_inst + 0.02 else "L0",
    )

    return FusionStepOutput(
        r_inst=r_inst,
        r_eff=r_eff,
        current_tier=current_tier,
        floor_tier=active_floor_tier,
        floor_score=active_floor_score,
        floor_expires_at=active_floor_exp,
        decayed_scores=decayed,
        attribution=attribution,
        synergy_triggered=synergy_triggered,
    )


def compute_mood_channel_score(
    mood_1_to_10: int,
    stress_0_to_10: int,
    sleep_hours: float,
    journal_negativity: float,
    prior_mood_risks: list[float] | None = None,
    closure_cue_detected: bool = False,
) -> tuple[float, bool, str]:
    """
    Compute x_mood in [0, 1] and detect Paradoxical Sudden Calm:
      x_mood = clip_[0,1](0.30 * u_stress + 0.25 * (1 - u_mood) + 0.20 * u_sleep_debt + 0.25 * u_journal_neg + delta_calm)
    Returns (x_mood, paradoxical_calm_detected, detail_vi).
    """
    u_stress = clip01(float(stress_0_to_10) / 10.0)
    u_mood = clip01((float(mood_1_to_10) - 1.0) / 9.0)
    # Optimal student sleep ~ 7.5h; debt rises sharply below 6.5h (or hypersomnia > 11h)
    if sleep_hours < 7.5:
        u_sleep_debt = clip01((7.5 - float(sleep_hours)) / 4.5)
    elif sleep_hours > 10.5:
        u_sleep_debt = clip01((float(sleep_hours) - 10.5) / 4.0 * 0.6)
    else:
        u_sleep_debt = 0.0
    u_journal_neg = clip01(journal_negativity)

    base_score = (
        0.30 * u_stress
        + 0.25 * (1.0 - u_mood)
        + 0.20 * u_sleep_debt
        + 0.25 * u_journal_neg
    )

    # Detect Paradoxical Sudden Calm: >= 5 consecutive prior severe records (risk >= 0.68)
    # followed by an abrupt jump to extreme calm (mood >= 8, stress <= 2) or closure cues
    prior = prior_mood_risks or []
    severe_streak = len(prior) >= 5 and all(r >= 0.68 for r in prior[:5])
    abrupt_calm = mood_1_to_10 >= 8 and stress_0_to_10 <= 2
    paradoxical = bool(severe_streak and (abrupt_calm or closure_cue_detected))

    delta_calm = 0.20 if paradoxical else 0.0
    x_mood = clip01(base_score + delta_calm)
    if paradoxical:
        # Ensure clinical visibility when sudden eerie calm follows >= 5 days of severe depression
        x_mood = max(x_mood, 0.58 if not closure_cue_detected else 0.72)

    if paradoxical:
        detail = "Phát hiện Bình thản Nghịch lý (đột ngột dịu bất thường sau chuỗi >=5 ngày suy sụp nặng)"
    elif x_mood >= 0.65:
        detail = f"Căng thẳng cao ({stress_0_to_10}/10), ngủ {sleep_hours:.1f}h & cảm xúc nặng nề"
    elif x_mood >= 0.42:
        detail = f"Năng lượng giảm (Mood {mood_1_to_10}/10, Stress {stress_0_to_10}/10, ngủ {sleep_hours:.1f}h)"
    else:
        detail = f"Tâm trạng ổn định (Mood {mood_1_to_10}/10, ngủ {sleep_hours:.1f}h)"

    return round(x_mood, 4), paradoxical, detail


def compute_clinical_channel_score(
    phq9_total: int | None = None,
    gad7_total: int | None = None,
    phq9_item9: int = 0,
    phq2_total: int | None = None,
    gad2_total: int | None = None,
) -> tuple[float, str]:
    """
    Compute x_clin in [0, 1] with PHQ-9 Item 9 Hard Clinical Anchor (alpha_q9):
      x_clin = max(0.65 * PHQ9/27 + 0.35 * GAD7/21, alpha_q9)
    Supports partial availability (PHQ9-only, GAD7-only, or PHQ2_GAD2 micro-screen).
    """
    q9_clamped = max(0, min(3, int(phq9_item9)))
    alpha_q9 = PHQ9_ITEM9_ANCHORS[q9_clamped]

    if phq9_total is not None and gad7_total is not None:
        composite = 0.65 * clip01(phq9_total / 27.0) + 0.35 * clip01(gad7_total / 21.0)
        label_parts = [f"PHQ-9={phq9_total}/27", f"GAD-7={gad7_total}/21"]
    elif phq9_total is not None:
        composite = clip01(phq9_total / 27.0)
        label_parts = [f"PHQ-9={phq9_total}/27"]
    elif gad7_total is not None:
        composite = clip01(gad7_total / 21.0)
        label_parts = [f"GAD-7={gad7_total}/21"]
    elif phq2_total is not None or gad2_total is not None:
        p2 = clip01((phq2_total or 0) / 6.0)
        g2 = clip01((gad2_total or 0) / 6.0)
        # Micro-screen caps composite at 0.62 before full PHQ-9/GAD-7 confirmation
        composite = min(0.62, 0.65 * p2 + 0.35 * g2)
        label_parts = [f"PHQ-2={phq2_total or 0}/6", f"GAD-2={gad2_total or 0}/6"]
    else:
        composite = 0.0
        label_parts = ["Chưa có bài sàng lọc"]

    if q9_clamped > 0:
        label_parts.append(f"Câu 9 PHQ-9={q9_clamped} (neo α={alpha_q9:.2f})")

    x_clin = round(clip01(max(composite, alpha_q9)), 4)
    return x_clin, ", ".join(label_parts)


def compute_xai_attribution(
    decayed_scores: dict[str, float],
    m_star: str,
    r_eff: float,
    r_inst: float,
    channel_details: dict[str, str],
    active_floor_tier: str = "L0",
) -> list[dict]:
    """
    Generate transparent XAI contribution breakdown summing cleanly to explain R_eff.
    """
    s_max = decayed_scores.get(m_star, 0.0)
    raw_contribs: dict[str, float] = {}
    for ch in CHANNELS:
        val = decayed_scores.get(ch, 0.0)
        if val <= 0.001:
            raw_contribs[ch] = 0.0
        elif ch == m_star:
            raw_contribs[ch] = val
        else:
            raw_contribs[ch] = ETA * (1.0 - s_max) * CHANNEL_WEIGHTS[ch] * val

    total_raw = sum(raw_contribs.values())
    items: list[dict] = []

    if total_raw <= 0.001:
        return [
            {
                "factor": CHANNEL_FACTORS_VI["mood"],
                "key": "mood",
                "contribution": round(r_eff, 3),
                "detail": channel_details.get("mood") or "Tâm trạng ổn định",
            }
        ]

    scale = (r_eff / total_raw) if total_raw > 0 else 1.0
    for ch in CHANNELS:
        c = round(raw_contribs[ch] * scale, 3)
        if c >= 0.01:
            detail = channel_details.get(ch) or "Đang theo dõi"
            if ch == m_star and active_floor_tier != "L0" and r_eff > r_inst + 0.02:
                detail = f"{detail} (Đang giữ khóa sàn an toàn {active_floor_tier})"
            items.append(
                {
                    "factor": CHANNEL_FACTORS_VI[ch],
                    "key": ch,
                    "contribution": c,
                    "detail": detail,
                }
            )

    items.sort(key=lambda x: x["contribution"], reverse=True)
    if not items:
        items.append(
            {
                "factor": CHANNEL_FACTORS_VI["mood"],
                "key": "mood",
                "contribution": round(r_eff, 3),
                "detail": channel_details.get("mood") or "Tâm trạng ổn định",
            }
        )
    return items
