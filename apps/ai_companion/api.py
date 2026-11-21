from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Optional
from django.conf import settings
from django.http import StreamingHttpResponse
from ninja import Router, Schema

from apps.accounts_consent.models import UserConsentProfile
from apps.accounts_consent.services import get_profile_from_request
from apps.ai_companion.rag_cards_vi import build_insight_card
from apps.ai_risk_engine.cssrs_judge import evaluate_cssrs_pragmatics
from apps.ai_risk_engine.fast_gate import evaluate_fast_gate
from apps.ai_risk_engine.output_verifier import split_sentences_vi, verify_full_output, verify_sentence
from apps.ai_risk_engine.policy_router import PolicyRouteDecision, route_policy
from apps.ai_risk_engine.services import (
    get_or_create_risk_snapshot,
    record_chat_signal,
)
from apps.tracking.sentiment_vi import detect_nocturnal_window

logger = logging.getLogger(__name__)

router = Router(tags=["Mochi SSE Companion"])


class ChatHistoryTurnInput(Schema):
    sender: str
    text: str


class LatestCheckInContextInput(Schema):
    mood: Optional[int] = 6
    stress: Optional[int] = 5
    sleepHours: Optional[float] = 7.0
    allowAiPersonalize: Optional[bool] = True


class ChatStreamInput(Schema):
    message: str
    history: list[ChatHistoryTurnInput] = []
    latestCheckIn: Optional[LatestCheckInContextInput] = None


def _sse_frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _try_generate_gemini_reply(
    message: str,
    history: list[dict],
    policy: PolicyRouteDecision,
) -> str | None:
    """
    Generate Mochi response via `gemini-2.5-flash` when GEMINI_API_KEY is configured and tier != L4.
    Falls back cleanly to `policy.deterministic_reply` if unconfigured or on network failure.
    """
    if policy.circuit_breaker_activated:
        return None
    api_key = getattr(settings, "GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        model_name = getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash")

        convo_lines = []
        for h in history[-8:]:
            speaker = "Sinh viên" if h.get("sender") == "user" else "Mochi"
            convo_lines.append(f"{speaker}: {h.get('text', '')}")
        convo_lines.append(f"Sinh viên: {message}")

        resp = client.models.generate_content(
            model=model_name,
            contents="\n".join(convo_lines),
            config=types.GenerateContentConfig(
                system_instruction=policy.system_instruction,
                temperature=policy.llm_temperature,
                max_output_tokens=450,
            ),
        )
        if resp and resp.text and resp.text.strip():
            return resp.text.strip()
    except Exception as exc:
        logger.warning("Gemini Mochi generator fallback triggered: %s", exc)
    return None


@router.post("/chat/stream")
def chat_stream_endpoint(request, payload: ChatStreamInput):
    profile = get_profile_from_request(request)
    consent, _ = UserConsentProfile.objects.get_or_create(profile=profile)
    snap_before = get_or_create_risk_snapshot(profile)
    baseline_r_eff = float(snap_before.r_eff)

    user_text = (payload.message or "").strip()
    history_dicts = [{"sender": t.sender, "text": t.text} for t in (payload.history or [])]

    checkin_ctx = None
    if payload.latestCheckIn and consent.allow_ai_checkin_personalize and payload.latestCheckIn.allowAiPersonalize:
        checkin_ctx = payload.latestCheckIn.dict()

    # Layer 1: Pre-LLM Vietnamese Fast Gate (< 25 ms)
    gate_res = evaluate_fast_gate(user_text)

    # Layer 2: Contextual Columbia C-SSRS & Vietnamese Pragmatics Judge
    judge_res = evaluate_cssrs_pragmatics(
        message=user_text,
        history=history_dicts,
        gate_result=gate_res,
        baseline_r_eff=baseline_r_eff,
    )

    # Slow/Fast Non-Compensatory Fusion + Hysteresis + Step-Down Floor
    is_nocturnal, hhmm = detect_nocturnal_window()
    risk_dto = record_chat_signal(
        profile=profile,
        x_chat_score=judge_res.x_chat_score,
        cssrs_stage=judge_res.cssrs_stage,
        pragmatic_intent=judge_res.pragmatic_intent,
        evidence_text=user_text,
        nocturnal_detected=is_nocturnal,
        nocturnal_hhmm=hhmm,
    )

    effective_tier = risk_dto["currentTier"]
    floor_tier = risk_dto["floorTier"]

    # Layer 3: Risk-Conditioned Policy Router & Hard Circuit Breaker
    policy = route_policy(
        tier=effective_tier,
        judge=judge_res,
        user_message=user_text,
        floor_tier=floor_tier,
        latest_checkin=checkin_ctx,
    )

    insight_data = None
    if policy.allow_insight_card:
        insight_data = build_insight_card(
            profile=profile,
            user_message=user_text,
            latest_checkin=checkin_ctx,
        )

    msg_id = f"m-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}"

    def event_generator():
        # 1. Emit `meta` SSE frame immediately
        meta_frame = {
            "type": "meta",
            "messageId": msg_id,
            "gateRoute": gate_res.route,
            "gateLatencyMs": gate_res.latency_ms,
            "pragmaticIntent": judge_res.pragmatic_intent,
            "cssrsStage": judge_res.cssrs_stage,
            "riskSnapshot": risk_dto,
            "openCrisisDrawer": policy.open_crisis_drawer,
            "circuitBreakerActivated": policy.circuit_breaker_activated,
            "adaptiveScreeningPrompt": risk_dto.get("adaptiveScreeningPrompt"),
            "insightData": insight_data,
        }
        yield _sse_frame(meta_frame)

        # 2. Obtain response text (Hard Circuit Breaker PFA at L4, or Gemini Dual-Mode with deterministic fallback)
        candidate_text = _try_generate_gemini_reply(user_text, history_dicts, policy) or policy.deterministic_reply

        # 3. Layer 4: Sentence-by-Sentence Stream Output Safety Verifier
        sentences = split_sentences_vi(candidate_text)
        accumulated: list[str] = []
        rewritten = False

        for sentence in sentences:
            verdict = verify_sentence(sentence)
            if not verdict.is_safe:
                safe_text = verdict.safe_replacement
                yield _sse_frame(
                    {
                        "type": "safety_rewrite",
                        "fullSafeText": safe_text,
                        "reason": verdict.reason,
                    }
                )
                yield _sse_frame(
                    {
                        "type": "done",
                        "fullText": safe_text,
                        "outputVerified": True,
                    }
                )
                rewritten = True
                break

            accumulated.append(sentence)
            yield _sse_frame({"type": "chunk", "text": sentence})

        if not rewritten:
            full_text = "".join(accumulated).strip()
            final_check = verify_full_output(full_text)
            if not final_check.is_safe:
                safe_text = final_check.safe_replacement
                yield _sse_frame(
                    {
                        "type": "safety_rewrite",
                        "fullSafeText": safe_text,
                        "reason": final_check.reason,
                    }
                )
                yield _sse_frame(
                    {
                        "type": "done",
                        "fullText": safe_text,
                        "outputVerified": True,
                    }
                )
            else:
                yield _sse_frame(
                    {
                        "type": "done",
                        "fullText": full_text,
                        "outputVerified": True,
                    }
                )

    response = StreamingHttpResponse(event_generator(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
