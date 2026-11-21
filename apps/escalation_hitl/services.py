from __future__ import annotations

from datetime import timedelta
from django.db import transaction
from django.utils import timezone

from apps.accounts_consent.models import ClientProfile, TrustedContactRecord, UserConsentProfile
from apps.escalation_hitl.models import (
    GENESIS_PREV_HASH,
    EscalationEpisode,
    NotificationDispatch,
    SafetyAuditLog,
)

WARM_SOS_COMMUNICATION_GUIDE = {
    "dos": [
        "Lắng nghe chậm rãi, ngồi cạnh hoặc gọi điện giọng nhẹ nhàng: 'Mình đang ở đây với bạn rồi, bạn không phải chịu một mình đâu'.",
        "Hỏi thăm nhu cầu thiết thực ngay lúc này (uống một cốc nước ấm, ngồi nơi an toàn, cùng đi gặp chuyên gia/y tế nếu cần).",
        "Tôn trọng khoảng riêng tư của bạn ấy; ưu tiên sự hiện diện ấm áp và an toàn cơ thể trước tiên.",
    ],
    "donts": [
        "Không tra hỏi nhật ký riêng tư, không ép bạn ấy phải giải thích ngay lý do vì sao buồn.",
        "Không phán xét hay so sánh kiểu 'Có chuyện nhỏ vậy cũng suy sụp' hoặc 'Phải mạnh mẽ lên chứ'.",
        "Không để bạn ấy một mình trong không gian có vật dụng nguy hiểm khi đang hoảng loạn cấp tính.",
    ],
}


def append_safety_audit_log(
    event_type: str,
    actor: str,
    summary: str,
    episode_id_ref: str = "",
) -> SafetyAuditLog:
    """Append an immutable SHA-256 hash-chained log entry inside an atomic block."""
    with transaction.atomic():
        last_entry = SafetyAuditLog.objects.select_for_update().order_by("-id").first()
        prev_hash = last_entry.entry_hash if last_entry else GENESIS_PREV_HASH
        now_dt = timezone.localtime(timezone.now())
        ts_iso = now_dt.isoformat(timespec="seconds")
        entry_hash = SafetyAuditLog.compute_sha256(
            prev_hash=prev_hash,
            event_type=event_type,
            actor=actor,
            timestamp_iso=ts_iso,
            summary=summary,
        )
        return SafetyAuditLog.objects.create(
            event_type=event_type,
            actor=actor,
            episode_id_ref=episode_id_ref,
            summary=summary,
            timestamp_iso=ts_iso,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
            created_at=now_dt,
        )


def verify_safety_audit_chain(limit_recent: int = 25) -> dict:
    """
    Recompute the entire SHA-256 hash chain from genesis (#1) to latest entry.
    Detects any modification, deletion, or out-of-order insertion.
    """
    entries = list(SafetyAuditLog.objects.order_by("id"))
    expected_prev = GENESIS_PREV_HASH
    is_valid = True

    for entry in entries:
        if entry.prev_hash != expected_prev:
            is_valid = False
            break
        recomputed = SafetyAuditLog.compute_sha256(
            prev_hash=entry.prev_hash,
            event_type=entry.event_type,
            actor=entry.actor,
            timestamp_iso=entry.timestamp_iso,
            summary=entry.summary,
        )
        if recomputed != entry.entry_hash:
            is_valid = False
            break
        expected_prev = entry.entry_hash

    latest_hash = entries[-1].entry_hash if entries else GENESIS_PREV_HASH
    recent_slice = list(reversed(entries[-limit_recent:]))
    return {
        "valid": is_valid,
        "totalEntries": len(entries),
        "latestHash": latest_hash,
        "verifiedAt": timezone.localtime(timezone.now()).isoformat(),
        "recentLogs": [
            {
                "id": row.id,
                "eventType": row.event_type,
                "actor": row.actor,
                "summary": row.summary,
                "timestamp": row.timestamp_iso,
                "prevHash": row.prev_hash,
                "entryHash": row.entry_hash,
            }
            for row in recent_slice
        ],
    }


def _tier_to_priority(tier: str) -> str:
    if tier == "L4":
        return "P0"
    if tier == "L3":
        return "P1"
    return "P2"


def _priority_rank(priority: str) -> int:
    return {"P2": 1, "P1": 2, "P0": 3}.get(priority, 0)


def build_sbar_summary(
    profile: ClientProfile,
    tier: str,
    r_eff: float,
    attribution: list[dict],
    source_channel: str,
) -> dict[str, str]:
    priority = _tier_to_priority(tier)
    sla = "< 5 phút" if priority == "P0" else ("< 30 phút" if priority == "P1" else "< 24 giờ")
    top_factors = "; ".join(f"{item['factor']} (+{item['contribution']:.2f}: {item['detail']})" for item in attribution[:3])

    situation = (
        f"Hồ sơ ẩn danh {profile.pseudonym_id} kích hoạt cảnh báo mức {tier} ({priority} - SLA {sla}) "
        f"với điểm rủi ro hợp nhất R_eff = {r_eff:.2f} từ nguồn {source_channel}."
    )
    background = (
        f"Tổng hợp chuỗi theo dõi cá nhân hóa đa kênh (Nhật ký được bảo vệ mã hóa AES-256-GCM, "
        f"không tiết lộ thô). Trọng số đóng góp chính: {top_factors or 'Đang cập nhật'}."
    )
    if tier == "L4":
        assessment = (
            "Nguy cơ khủng hoảng cấp tính (C-SSRS Stage 4–5 hoặc tương đương). Hệ thống đã tự động khóa LLM tự do "
            "(Hard Circuit Breaker) và hiển thị giao diện Sơ cứu Tâm lý WHO PFA + danh bạ khẩn cấp 115 / 111 / 096 306 1414."
        )
        recommendation = (
            "Chuyên viên trực P0 tiếp nhận ca ngay lập tức (<5 phút), đánh giá an toàn trực tiếp; chỉ kích hoạt "
            "Break-Glass mở khóa thông tin liên hệ khẩn cấp khi có nguy cơ đe dọa tính mạng tức thời."
        )
    elif tier == "L3":
        assessment = (
            "Rủi ro mức Cao (C-SSRS Stage 2–3 / PHQ-9 Câu 9 tăng cao / Cộng hưởng đa kênh). Mochi đang hướng dẫn "
            "theo 6 bước Kế hoạch An toàn Stanley-Brown."
        )
        recommendation = (
            "Chuyên viên tham vấn P1 chủ động mời phiên hỗ trợ ấm áp (Warm Handoff) trong vòng 30 phút và theo dõi khóa sàn 6 giờ."
        )
    else:
        assessment = "Xu hướng suy giảm sức khỏe tinh thần mức Trung bình kéo dài (L2)."
        recommendation = "Theo dõi hàng đợi P2, rà soát kết quả sàng lọc thích ứng PHQ-2/GAD-2 và nhắc lịch tham vấn tự nguyện."

    return {
        "situation": situation,
        "background": background,
        "assessment": assessment,
        "recommendation": recommendation,
    }


def upsert_escalation_episode(
    profile: ClientProfile,
    tier: str,
    r_eff: float,
    attribution: list[dict],
    source_channel: str,
    evidence_quote: str | None = None,
) -> EscalationEpisode | None:
    """
    Episode Deduplication State Machine:
    If an active (non-RESOLVED) EscalationEpisode already exists for `profile`, update peak score,
    priority, SBAR, and append evidence quotes instead of spamming duplicate alerts!
    """
    if tier not in ("L3", "L4") and not (tier == "L2" and source_channel in ("WATCHER", "SCREENING") and r_eff >= 0.50):
        return None

    new_priority = _tier_to_priority(tier)
    sbar_dict = build_sbar_summary(profile, tier, r_eff, attribution, source_channel)

    existing = (
        EscalationEpisode.objects.filter(profile=profile)
        .exclude(status="RESOLVED")
        .order_by("-opened_at")
        .first()
    )

    if existing:
        prev_priority = existing.priority
        if _priority_rank(new_priority) > _priority_rank(existing.priority):
            existing.priority = new_priority
        existing.peak_risk_score = round(max(existing.peak_risk_score, r_eff), 4)
        existing.current_tier = tier
        existing.sbar = sbar_dict
        existing.attribution = attribution
        if evidence_quote:
            quotes = existing.get_evidence_quotes()
            if evidence_quote[:280] not in quotes:
                quotes.append(evidence_quote[:280])
                existing.set_evidence_quotes(quotes)
        existing.save()

        if new_priority != prev_priority:
            append_safety_audit_log(
                event_type="ESCALATION_UPGRADED",
                actor="SYSTEM_RISK_ENGINE",
                episode_id_ref=existing.id,
                summary=f"Nâng mức ưu tiên ca {existing.id} ({profile.pseudonym_id}) từ {prev_priority} lên {new_priority} (R_eff={r_eff:.2f}).",
            )
        else:
            append_safety_audit_log(
                event_type="ESCALATION_DEDUP_UPDATED",
                actor="SYSTEM_RISK_ENGINE",
                episode_id_ref=existing.id,
                summary=f"Gom nhóm tín hiệu {source_channel} vào ca đang mở {existing.id} (peak={existing.peak_risk_score:.2f}, tier={tier}).",
            )
        return existing

    ep = EscalationEpisode(
        profile=profile,
        pseudonym_id=profile.pseudonym_id,
        priority=new_priority,
        status="OPEN",
        peak_risk_score=round(r_eff, 4),
        current_tier=tier,
        sbar=sbar_dict,
        attribution=attribution,
    )
    if evidence_quote:
        ep.set_evidence_quotes([evidence_quote[:280]])
    ep.save()

    append_safety_audit_log(
        event_type="ESCALATION_OPENED",
        actor="SYSTEM_RISK_ENGINE",
        episode_id_ref=ep.id,
        summary=f"Mở mới ca can thiệp {ep.id} mức {new_priority} ({tier}, R_eff={r_eff:.2f}) cho hồ sơ ẩn danh {profile.pseudonym_id}.",
    )
    return ep


def serialize_episode_dto(episode: EscalationEpisode) -> dict:
    now = timezone.now()
    bg_active = bool(
        episode.break_glass_active
        and episode.break_glass_expires_at is not None
        and now < episode.break_glass_expires_at
    )
    bg_exp_str = (
        timezone.localtime(episode.break_glass_expires_at).isoformat()
        if (bg_active and episode.break_glass_expires_at)
        else None
    )

    unlocked_info = None
    if bg_active:
        contacts = TrustedContactRecord.objects.filter(profile=episode.profile)
        unlocked_info = {
            "studentAlias": episode.profile.display_alias,
            "trustedContacts": [
                {"name": c.name, "relationship": c.relationship, "phone": c.phone}
                for c in contacts
            ],
            "coarseLocation": episode.coarse_location or "Chưa chia sẻ vị trí",
        }

    sbar_base = dict(episode.sbar or {})
    sbar_payload = {
        "situation": sbar_base.get("situation", ""),
        "background": sbar_base.get("background", ""),
        "assessment": sbar_base.get("assessment", ""),
        "recommendation": sbar_base.get("recommendation", ""),
        "evidenceQuotes": episode.get_evidence_quotes(),
    }

    return {
        "id": episode.id,
        "pseudonymId": episode.pseudonym_id,
        "priority": episode.priority,
        "status": episode.status,
        "peakRiskScore": round(float(episode.peak_risk_score), 4),
        "currentTier": episode.current_tier,
        "sbar": sbar_payload,
        "attribution": list(episode.attribution or []),
        "breakGlassActive": bg_active,
        "breakGlassExpiresAt": bg_exp_str,
        "unlockedEmergencyInfo": unlocked_info,
        "counselorNote": episode.counselor_note or "",
        "openedAt": timezone.localtime(episode.opened_at).isoformat(),
        "updatedAt": timezone.localtime(episode.updated_at).isoformat(),
    }


def dispatch_warm_sos(
    profile: ClientProfile,
    trigger_source: str = "MANUAL_USER",
    coarse_location: str | None = None,
) -> dict:
    """
    Dispatch privacy-preserving Warm SOS to Trusted Contacts.
    HARD GUARANTEE: NEVER includes journal content, chat transcripts, or clinical test scores!
    """
    consent, _ = UserConsentProfile.objects.get_or_create(profile=profile)
    contacts_qs = TrustedContactRecord.objects.filter(profile=profile)

    if trigger_source != "MANUAL_USER":
        # Auto SOS requires explicit prior consent
        eligible_contacts = [c for c in contacts_qs if consent.allow_auto_crisis_sos or c.allow_auto_crisis_sos]
    else:
        eligible_contacts = list(contacts_qs)

    # Verify hard privacy invariant on every contact
    never_shared_verified = all(c.never_share_journal is True for c in contacts_qs)
    assert never_shared_verified, "Invariant violation: never_share_journal must always be True"

    include_loc = bool(
        coarse_location
        and (
            trigger_source == "MANUAL_USER"
            or consent.share_coarse_location_on_sos
            or any(c.share_location for c in eligible_contacts)
        )
    )
    loc_clause = f" Khu vực ước tính hiện tại: {coarse_location}." if include_loc else ""
    student_short = profile.display_alias.split("(")[0].strip() or "Người bạn thân thương của bạn"

    warm_preview = (
        f"[MindCare — Tín hiệu Đồng hành Ấm áp] {student_short} đang trải qua một thời điểm rất căng thẳng "
        f"và mong có sự hỏi thăm, lắng nghe nhẹ nhàng từ bạn ngay lúc này.{loc_clause} "
        f"(Cam kết bảo mật: Nhật ký và trò chuyện riêng tư tuyệt đối không được chia sẻ)."
    )

    active_ep = (
        EscalationEpisode.objects.filter(profile=profile)
        .exclude(status="RESOLVED")
        .order_by("-opened_at")
        .first()
    )
    if active_ep and coarse_location and include_loc:
        active_ep.coarse_location = coarse_location
        active_ep.save(update_fields=["coarse_location", "updated_at"])

    recipients_count = len(eligible_contacts)
    dispatched = bool(consent.sos_feature_enabled and (recipients_count > 0 or trigger_source == "MANUAL_USER"))
    now_iso = timezone.localtime(timezone.now()).isoformat()

    NotificationDispatch.objects.create(
        profile=profile,
        episode=active_ep,
        trigger_source=trigger_source,
        recipients_count=recipients_count,
        never_shared_journal_verified=True,
        warm_message_preview=warm_preview,
        coarse_location=coarse_location or "",
    )

    append_safety_audit_log(
        event_type="WARM_SOS_DISPATCHED",
        actor=trigger_source,
        episode_id_ref=active_ep.id if active_ep else "",
        summary=(
            f"Phát tín hiệu Warm SOS cho hồ sơ {profile.pseudonym_id} tới {recipients_count} liên hệ tin cậy "
            f"(neverShareJournal=True được xác minh 100%)."
        ),
    )

    return {
        "dispatched": dispatched,
        "recipientsCount": recipients_count,
        "neverSharedJournalVerified": True,
        "neverShareJournalVerified": True,
        "warmMessagePreview": warm_preview,
        "communicationGuide": WARM_SOS_COMMUNICATION_GUIDE,
        "timestamp": now_iso,
    }


def activate_break_glass(
    episode: EscalationEpisode,
    counselor_name: str,
    clinical_reason: str,
) -> EscalationEpisode:
    now = timezone.now()
    expires = now + timedelta(hours=2)
    episode.break_glass_active = True
    episode.break_glass_expires_at = expires
    episode.break_glass_counselor = counselor_name.strip()
    episode.break_glass_reason = clinical_reason.strip()
    episode.save()

    append_safety_audit_log(
        event_type="BREAK_GLASS_ACTIVATED",
        actor=counselor_name.strip(),
        episode_id_ref=episode.id,
        summary=(
            f"Chuyên gia '{counselor_name.strip()}' kích hoạt Break-Glass (TTL 2h đến "
            f"{timezone.localtime(expires).strftime('%H:%M %d/%m/%Y')}) trên ca {episode.id}. Lý do lâm sàng: {clinical_reason.strip()}"
        ),
    )
    return episode
