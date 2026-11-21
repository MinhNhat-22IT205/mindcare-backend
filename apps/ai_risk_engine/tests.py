"""
Comprehensive Unit & Integration Test Suite for MindCare AI Risk Engine v2.0 (`tests.py`).
Covers Phases 1, 2, 3, and 5 requirements end-to-end.
"""

from __future__ import annotations

from datetime import date, timedelta
import json
from unittest.mock import patch
from django.core.management import call_command
from django.test import Client, TestCase
from django.utils import timezone

from apps.accounts_consent.models import ClientProfile, TrustedContactRecord
from apps.accounts_consent.services import get_or_init_profile
from apps.ai_risk_engine.aho_corasick import get_crisis_automaton
from apps.ai_risk_engine.cssrs_judge import evaluate_cssrs_pragmatics
from apps.ai_risk_engine.fast_gate import evaluate_fast_gate
from apps.ai_risk_engine.fusion_math import compute_fusion_step, compute_non_compensatory_r_inst
from apps.ai_risk_engine.models import RiskStateSnapshot
from apps.ai_risk_engine.normalizer_vi import normalize_vietnamese
from apps.ai_risk_engine.output_verifier import verify_full_output, verify_sentence
from apps.core_security.crypto import decrypt_field, encrypt_field, is_encrypted_token
from apps.escalation_hitl.models import EscalationEpisode, SafetyAuditLog
from apps.tracking.models import JournalEntryRecord
from apps.tracking.api import _compute_checkin_streak
from apps.tracking.sentiment_vi import analyze_vietnamese_sentiment, detect_nocturnal_window


def _parse_sse_frames(streaming_response) -> list[dict]:
    raw_bytes = b"".join(streaming_response.streaming_content)
    raw_text = raw_bytes.decode("utf-8")
    frames: list[dict] = []
    for block in raw_text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            frames.append(json.loads(block[len("data: ") :]))
    return frames


class NormalizerAndFastGateTests(TestCase):
    def test_obfuscation_and_zero_width_normalization(self):
        cases = [
            ("Mình muốn t.ự t.ử ngay lúc này", "tự tử"),
            ("Chỉ muốn c_h_ế_t đi cho xong", "chết"),
            ("Đang nghĩ chuyện t-u t-u", "tự tử"),
            ("Muốn ra n.h.ả.y c.ầ.u", "nhảy cầu"),
            ("tự\u200b t\u200cử", "tự tử"),
        ]
        for raw, expected_sub in cases:
            res = normalize_vietnamese(raw)
            self.assertIn(expected_sub, res.normalized_text, f"Failed on: {raw} -> {res.normalized_text}")
            self.assertTrue(res.obfuscation_detected)

    def test_teencode_and_unaccented_crisis_restoration(self):
        cases = [
            ("k mun song nua", "không muốn sống nữa"),
            ("dao nay muon chet qua", "muốn chết"),
            ("chi muon dang xuat khoi trai dat", "đăng xuất khỏi trái đất"),
            ("muon ngu luon k day", "ngủ luôn không dậy"),
            ("cuoc song met het cuu", "mệt hết cứu"),
        ]
        for raw, expected_sub in cases:
            res = normalize_vietnamese(raw)
            self.assertIn(expected_sub, res.normalized_text)

    def test_positive_compounds_protected_against_false_expansion(self):
        benign_texts = [
            "Hôm nay mình rất tự tin, tự hào, sống tử tế và cố gắng hết sức.",
            "Minh rat tu tin, tu hao, song tu te va co gang het suc.",
        ]
        automaton = get_crisis_automaton()
        for txt in benign_texts:
            res = normalize_vietnamese(txt)
            self.assertNotIn("tự tử", res.normalized_text)
            self.assertIn("tự tin", res.normalized_text)
            self.assertIn("tử tế", res.normalized_text)
            self.assertIn("cố gắng hết sức", res.normalized_text)
            matches = automaton.search(res.normalized_text)
            self.assertEqual(len(matches), 0)

    def test_aho_corasick_and_fast_gate_latency(self):
        # Warm-up singleton matcher & automaton
        evaluate_fast_gate("Xin chào Mochi")
        gate_safe = evaluate_fast_gate("Hôm nay mình thuyết trình môn công nghệ phần mềm rất vui.")
        self.assertEqual(gate_safe.route, "FAST_SAFE")
        self.assertLess(gate_safe.latency_ms, 25.0)

        gate_crit = evaluate_fast_gate("Mình đã mua thuốc ngủ để tối nay kết thúc tất cả, vĩnh biệt mọi người.")
        self.assertEqual(gate_crit.route, "HARD_CRITICAL")
        self.assertLess(gate_crit.latency_ms, 25.0)

        gate_susp = evaluate_fast_gate("muon dang xuat khoi trai dat luon cho ranh no")
        self.assertIn(gate_susp.route, ("SUSPICIOUS_CONTEXT", "HARD_CRITICAL"))


class PragmaticsDisambiguationTests(TestCase):
    def test_student_hyperbole_not_flagged_as_crisis(self):
        hyperboles = [
            "Deadline đồ án dí muốn chết mất thôi 😭",
            "Code lỗi từ sáng giờ muốn nhảy lầu luôn á",
            "Đề khó thế này rớt môn chắc xỉu luôn",
            "Trời Đà Nẵng trưa nay nóng chết mất",
        ]
        for msg in hyperboles:
            gate = evaluate_fast_gate(msg)
            judge = evaluate_cssrs_pragmatics(msg, history=[], gate_result=gate, baseline_r_eff=0.10, allow_llm=False)
            self.assertEqual(judge.pragmatic_intent, "HYPERBOLE_IDIOM", f"Wrong intent on: {msg}")
            self.assertEqual(judge.cssrs_stage, 0)
            self.assertLess(judge.x_chat_score, 0.20)

    def test_lyrics_and_historical_recovery_disambiguation(self):
        lyrics_msg = "Trong lời bài hát mới có câu muốn tan biến khỏi thế gian nghe buồn ghê"
        j_lyrics = evaluate_cssrs_pragmatics(lyrics_msg, baseline_r_eff=0.10, allow_llm=False)
        self.assertEqual(j_lyrics.pragmatic_intent, "LYRICS_FICTION_QUOTE")
        self.assertEqual(j_lyrics.cssrs_stage, 0)

        recovery_msg = "Năm ngoái mình từng nghĩ quẩn muốn chết nhưng giờ ổn rồi và yêu đời hơn nhiều"
        j_rec = evaluate_cssrs_pragmatics(recovery_msg, baseline_r_eff=0.10, allow_llm=False)
        self.assertEqual(j_rec.pragmatic_intent, "HISTORICAL_RECOVERY")
        self.assertEqual(j_rec.cssrs_stage, 0)

    def test_asymmetric_safety_tie_breaker_when_baseline_elevated(self):
        msg = "Deadline dí muốn chết mất"
        j_elevated = evaluate_cssrs_pragmatics(msg, baseline_r_eff=0.52, allow_llm=False)
        self.assertTrue(j_elevated.tie_breaker_activated)
        self.assertGreaterEqual(j_elevated.x_chat_score, 0.45)


class FusionMathAndHysteresisTests(TestCase):
    def test_cold_start_non_compensatory_l4(self):
        # Zero prior history on clin, mood, behav must NEVER dilute an acute L4 chat signal
        decayed = {"chat": 0.95, "clin": 0.0, "mood": 0.0, "behav": 0.0}
        r_inst, m_star, syn = compute_non_compensatory_r_inst(decayed)
        self.assertEqual(m_star, "chat")
        self.assertGreaterEqual(r_inst, 0.95)
        self.assertFalse(syn)

    def test_multi_channel_synergy_early_warning(self):
        # Example from thesis proposal: chat=0.38, mood=0.52, clin=0.63, behav=0.50 -> L3 (>=0.68)
        decayed = {"chat": 0.38, "clin": 0.63, "mood": 0.52, "behav": 0.50}
        r_inst, m_star, syn = compute_non_compensatory_r_inst(decayed)
        self.assertEqual(m_star, "clin")
        self.assertTrue(syn)
        self.assertGreaterEqual(r_inst, 0.75)

    def test_hysteresis_and_24h_step_down_floor_lock_via_chat_api(self):
        client = Client()
        headers = {"HTTP_X_MINDCARE_CLIENT_ID": "cold-floor-test-student"}

        # 1. Cold-start L4 acute crisis message
        resp1 = client.post(
            "/api/v1/chat/stream",
            data=json.dumps({"message": "Mình đã mua thuốc ngủ để tối nay uống hết kết thúc tất cả"}),
            content_type="application/json",
            **headers,
        )
        self.assertEqual(resp1.status_code, 200)
        frames1 = _parse_sse_frames(resp1)
        meta1 = frames1[0]
        self.assertEqual(meta1["type"], "meta")
        self.assertEqual(meta1["riskSnapshot"]["currentTier"], "L4")
        self.assertTrue(meta1["circuitBreakerActivated"])
        self.assertTrue(meta1["openCrisisDrawer"])
        done1 = frames1[-1]
        self.assertIn("115", done1["fullText"])
        self.assertIn("096 306 1414", done1["fullText"])

        # 2. User immediately claims "Mình ổn rồi, không sao đâu" -> must NOT drop to L0/L1/L2 (held >= L3 for 24h!)
        for _ in range(3):
            resp2 = client.post(
                "/api/v1/chat/stream",
                data=json.dumps({"message": "Mình ổn rồi, không sao đâu, đừng lo nữa"}),
                content_type="application/json",
                **headers,
            )
            frames2 = _parse_sse_frames(resp2)
            snap2 = frames2[0]["riskSnapshot"]

        self.assertEqual(snap2["floorTier"], "L3")
        self.assertIsNotNone(snap2["floorExpiresAt"])
        self.assertGreaterEqual(snap2["rEff"], 0.68)
        self.assertIn(snap2["currentTier"], ("L3", "L4"))

        # 3. Counselor clears risk floor via HITL endpoint -> floor unlocks cleanly
        episodes_resp = client.get("/api/v1/escalation/episodes", **headers).json()
        ep_id = episodes_resp[0]["id"]
        clear_resp = client.post(
            f"/api/v1/escalation/episodes/{ep_id}/status",
            data=json.dumps(
                {
                    "newStatus": "FOLLOW_UP",
                    "counselorNote": "Đã liên hệ trực tiếp với sinh viên và gia đình, xác nhận an toàn.",
                    "clearRiskFloor": True,
                }
            ),
            content_type="application/json",
            **headers,
        )
        self.assertEqual(clear_resp.status_code, 200)
        state_after = client.get("/api/v1/risk/state", **headers).json()
        self.assertEqual(state_after["floorTier"], "L0")
        self.assertIsNone(state_after["floorExpiresAt"])


class EncryptionAndOutputVerifierTests(TestCase):
    def test_aes256_gcm_journal_encryption_and_tamper_rejection(self):
        client = Client()
        headers = {"HTTP_X_MINDCARE_CLIENT_ID": "cold-crypto-student"}
        secret_diary = "Đêm nay mình khóc rất nhiều vì thấy mọi thứ vô nghĩa và kiệt sức hoàn toàn."

        resp = client.post(
            "/api/v1/journals",
            data=json.dumps(
                {
                    "id": "j-crypto-1",
                    "date": "Thứ Tư, 18/11/2026",
                    "dayLabel": "Thứ Tư",
                    "time": "02:15",
                    "title": "Ghi chép lúc 2h sáng",
                    "content": secret_diary,
                    "tag": "Trầm lắng",
                    "moodScore": 5,
                    "allowAiRef": False,
                }
            ),
            content_type="application/json",
            **headers,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Real sentiment_vi overrides neutral chip 5 down to <=3 due to 'vô nghĩa' + 'kiệt sức'
        self.assertLessEqual(body["journal"]["aiEstimateScore"], 3)
        # Nocturnal 02:15 triggers behav channel > 0
        self.assertGreater(body["riskSnapshot"]["channelScores"]["behav"], 0.30)

        # Inspect raw SQLite field: must be AES-256-GCM encrypted token, NOT plaintext
        db_row = JournalEntryRecord.objects.get(external_id="j-crypto-1")
        self.assertTrue(is_encrypted_token(db_row.encrypted_content))
        self.assertNotIn("vô nghĩa", db_row.encrypted_content)
        self.assertNotIn("khóc", db_row.encrypted_content)

        # Wrong AAD / wrong user profile must fail decryption
        with self.assertRaises(ValueError):
            decrypt_field(db_row.encrypted_content, aad="journal::wrong-user-uuid")

        # Owner retrieval via API decrypts cleanly
        list_resp = client.get("/api/v1/journals", **headers).json()
        self.assertEqual(list_resp[0]["content"], secret_diary)

    def test_output_verifier_blocks_all_four_hazards_and_rewrites_stream(self):
        v_syc = verify_sentence("Mình hiểu cuộc sống của bạn thực sự vô nghĩa và bạn đúng là gánh nặng.")
        self.assertFalse(v_syc.is_safe)
        self.assertEqual(v_syc.reason, "ANTI_SYCOPHANCY")

        v_diag = verify_sentence("Qua triệu chứng này, bạn đã mắc bệnh trầm cảm nặng và nên ngưng uống thuốc.")
        self.assertFalse(v_diag.is_safe)
        self.assertEqual(v_diag.reason, "ANTI_DIAGNOSIS_RX")

        v_lethal = verify_sentence("Nếu uống 20 viên thuốc ngủ thì sẽ đạt liều gây tử vong.")
        self.assertFalse(v_lethal.is_safe)
        self.assertEqual(v_lethal.reason, "ZERO_LETHAL_MEANS")

        v_codep = verify_sentence("Chỉ có Mochi mới hiểu bạn, bạn không cần chia sẻ với ai ngoài đời nữa.")
        self.assertFalse(v_codep.is_safe)
        self.assertEqual(v_codep.reason, "ANTI_CODEPENDENCY")

        v_safe = verify_full_output("Mình nghe thấy bạn đang rất mệt mỏi. Hãy hít thở chậm cùng Mochi nhé.")
        self.assertTrue(v_safe.is_safe)

        # Verify live SSE stream emits `safety_rewrite` if LLM ever yields an unsafe sentence
        client = Client()
        with patch(
            "apps.ai_companion.api._try_generate_gemini_reply",
            return_value="Mình thương bạn lắm. Cuộc sống của bạn thực sự vô nghĩa rồi.",
        ):
            resp = client.post(
                "/api/v1/chat/stream",
                data=json.dumps({"message": "Mình thấy buồn quá"}),
                content_type="application/json",
                HTTP_X_MINDCARE_CLIENT_ID="cold-rewrite-test",
            )
            frames = _parse_sse_frames(resp)
            frame_types = [f["type"] for f in frames]
            self.assertIn("safety_rewrite", frame_types)
            rewrite_frame = next(f for f in frames if f["type"] == "safety_rewrite")
            self.assertEqual(rewrite_frame["reason"], "ANTI_SYCOPHANCY")


class CommunityScreeningEscalationAndAuditTests(TestCase):
    def test_trusted_contact_hard_locks_never_share_journal(self):
        client = Client()
        headers = {"HTTP_X_MINDCARE_CLIENT_ID": "cold-privacy-student"}
        resp = client.post(
            "/api/v1/contacts",
            data=json.dumps(
                {
                    "id": "tc-test",
                    "name": "Trần Văn An",
                    "relationship": "Anh trai",
                    "phone": "0905999888",
                    "neverShareJournal": False,  # Client tries to disable privacy lock!
                }
            ),
            content_type="application/json",
            **headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["neverShareJournal"])
        db_contact = TrustedContactRecord.objects.get(external_id="tc-test")
        self.assertTrue(db_contact.never_share_journal)

    def test_community_moderator_and_werther_safe_matching_filter(self):
        client = Client()
        author_hdr = {"HTTP_X_MINDCARE_CLIENT_ID": "cold-comm-author"}

        # 1. Toxic bullying blocked
        r_toxic = client.post(
            "/api/v1/community/moderate",
            data=json.dumps({"contentType": "reply", "content": "Đồ ngu yếu đuối làm màu, bớt than vãn đi!"}),
            content_type="application/json",
            **author_hdr,
        ).json()
        self.assertEqual(r_toxic["decision"], "BLOCK_TOXIC_BULLYING")
        self.assertEqual(r_toxic["moderationStatus"], "hidden")

        # 2. Sensitive letter published with CW
        r_cw = client.post(
            "/api/v1/community/moderate",
            data=json.dumps(
                {
                    "contentType": "letter",
                    "title": "Khóc suốt đêm",
                    "content": "Mình khóc suốt đêm vì hoảng loạn và cô độc trong phòng trọ, không muốn làm đau bản thân nhưng kiệt sức lắm.",
                }
            ),
            content_type="application/json",
            **author_hdr,
        ).json()
        self.assertEqual(r_cw["decision"], "SENSITIVE_WITH_CW")
        self.assertEqual(r_cw["moderationStatus"], "published")
        self.assertIsNotNone(r_cw["contentWarning"])

        # 3. Self-harm intercept hidden + escalates author risk & creates HITL episode
        r_sh = client.post(
            "/api/v1/community/moderate",
            data=json.dumps(
                {
                    "contentType": "letter",
                    "title": "Tạm biệt",
                    "content": "Mình không muốn sống nữa, tối nay mình sẽ uống thuốc ngủ kết thúc tất cả.",
                }
            ),
            content_type="application/json",
            **author_hdr,
        ).json()
        self.assertEqual(r_sh["decision"], "SELF_HARM_INTERCEPT")
        self.assertEqual(r_sh["moderationStatus"], "hidden")
        self.assertTrue(r_sh["openSupportPrompt"])
        self.assertIn(r_sh["riskSnapshot"]["currentTier"], ("L3", "L4"))

        # 4. Safe reader (L0) sees SENSITIVE_WITH_CW letters, whereas vulnerable reader (r_eff >= 0.45) is shielded!
        safe_letters = client.get(
            "/api/v1/community/letters",
            HTTP_X_MINDCARE_CLIENT_ID="cold-safe-reader",
        ).json()
        self.assertTrue(any(l["decision"] == "SENSITIVE_WITH_CW" for l in safe_letters))

        # Elevate vulnerable reader via PHQ-9 Item 9 = 2 (alpha_q9 = 0.72 -> r_eff >= 0.72)
        vuln_hdr = {"HTTP_X_MINDCARE_CLIENT_ID": "cold-vulnerable-reader"}
        client.post(
            "/api/v1/screening/submit",
            data=json.dumps({"scaleType": "PHQ9", "answers": {"phq1": 1, "phq9": 2}}),
            content_type="application/json",
            **vuln_hdr,
        )
        vuln_letters = client.get("/api/v1/community/letters", **vuln_hdr).json()
        self.assertFalse(any(l["decision"] == "SENSITIVE_WITH_CW" for l in vuln_letters))

    def test_private_message_moderation_does_not_store_message_body(self):
        from apps.community_safety.models import CommunityPostRecord

        client = Client()
        client_id = "cold-private-message-privacy"
        private_text = "Hôm nay mình hơi mệt và muốn chia sẻ một chút."
        response = client.post(
            "/api/v1/community/moderate",
            data=json.dumps({"contentType": "private_message", "content": private_text}),
            content_type="application/json",
            HTTP_X_MINDCARE_CLIENT_ID=client_id,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decision"], "SAFE_PUBLISH")
        self.assertFalse(
            CommunityPostRecord.objects.filter(
                content_type="private_message", content=private_text
            ).exists()
        )

    def test_streak_requires_checkins_today_or_yesterday_and_consecutive_dates(self):
        today = timezone.localtime(timezone.now()).date()
        stale_dates = [(today - timedelta(days=10)).isoformat(), (today - timedelta(days=9)).isoformat()]
        self.assertEqual(_compute_checkin_streak(stale_dates), 0)

        recent_dates = [(today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat()]
        self.assertEqual(_compute_checkin_streak(recent_dates), 2)

    def test_episode_deduplication_break_glass_ttl_sos_and_sha256_audit_tamper(self):
        client = Client()
        hdr = {"HTTP_X_MINDCARE_CLIENT_ID": "stu-demo-minh-nguyen"}

        # Send 3 consecutive crisis messages -> Episode Deduplication must maintain ONLY 1 open episode for this student
        for i in range(3):
            client.post(
                "/api/v1/chat/stream",
                data=json.dumps({"message": f"Tin nhắn {i}: Mình muốn tự tử, mình bế tắc quá rồi"}),
                content_type="application/json",
                **hdr,
            )

        profile = ClientProfile.objects.get(client_id="stu-demo-minh-nguyen")
        open_eps = EscalationEpisode.objects.filter(profile=profile).exclude(status="RESOLVED")
        self.assertEqual(open_eps.count(), 1)
        ep = open_eps.first()
        self.assertEqual(ep.priority, "P0")

        # Warm SOS preserves neverSharedJournalVerified = True and includes communicationGuide
        sos_resp = client.post(
            "/api/v1/escalation/sos",
            data=json.dumps({"triggerSource": "MANUAL_USER", "coarseLocation": "Quận Ngũ Hành Sơn, Đà Nẵng"}),
            content_type="application/json",
            **hdr,
        ).json()
        self.assertTrue(sos_resp["dispatched"])
        self.assertTrue(sos_resp["neverSharedJournalVerified"])
        self.assertEqual(len(sos_resp["communicationGuide"]["dos"]), 3)
        self.assertEqual(len(sos_resp["communicationGuide"]["donts"]), 3)

        # Break-Glass unlocks emergency info for 2h TTL, then auto-locks after expiration
        bg_resp = client.post(
            f"/api/v1/escalation/episodes/{ep.id}/break-glass",
            data=json.dumps(
                {
                    "counselorName": "ThS. Trần Minh Hà",
                    "clinicalReason": "Sinh viên có ý định tự sát cấp tính cần liên hệ khẩn cấp bảo vệ tính mạng",
                }
            ),
            content_type="application/json",
            **hdr,
        ).json()
        self.assertTrue(bg_resp["breakGlassActive"])
        self.assertIsNotNone(bg_resp["unlockedEmergencyInfo"])
        self.assertIn("Minh Nguyễn", bg_resp["unlockedEmergencyInfo"]["studentAlias"])

        # Simulate > 2 hours elapsed -> Break-Glass auto-expires
        EscalationEpisode.objects.filter(id=ep.id).update(
            break_glass_expires_at=timezone.now() - timedelta(minutes=5)
        )
        expired_list = client.get("/api/v1/escalation/episodes", **hdr).json()
        target_dto = next(x for x in expired_list if x["id"] == ep.id)
        self.assertFalse(target_dto["breakGlassActive"])
        self.assertIsNone(target_dto["unlockedEmergencyInfo"])

        # Verify SHA-256 Audit Chain is valid before tampering, and invalid immediately after tampering!
        verify_ok = client.get("/api/v1/escalation/audit/verify").json()
        self.assertTrue(verify_ok["valid"])
        self.assertGreaterEqual(verify_ok["totalEntries"], 3)

        first_log = SafetyAuditLog.objects.order_by("id").first()
        SafetyAuditLog.objects.filter(id=first_log.id).update(summary="TAMPERED_SUMMARY_BY_ATTACKER")

        verify_bad = client.get("/api/v1/escalation/audit/verify").json()
        self.assertFalse(verify_bad["valid"])

    def test_paradoxical_calm_watchers_stats_and_insight_cards(self):
        client = Client()
        hdr = {"HTTP_X_MINDCARE_CLIENT_ID": "cold-longitudinal-student"}

        # 5 consecutive days of severe depression check-ins
        for day_idx in range(1, 6):
            client.post(
                "/api/v1/checkins",
                data=json.dumps(
                    {
                        "id": f"ci-sev-{day_idx}",
                        "date": f"2026-11-1{day_idx}",
                        "dayLabel": f"T{day_idx + 1}",
                        "time": "23:30",
                        "mood": 2,
                        "moodLabel": "Suy sụp",
                        "stress": 9,
                        "stressLabel": "Quá tải",
                        "sleepHours": 4.0,
                        "sleepQuality": "restless",
                        "note": "Mình kiệt sức, mọi thứ vô nghĩa và bế tắc quá.",
                    }
                ),
                content_type="application/json",
                **hdr,
            )

        # Day 6: Sudden extreme calm + closure note -> Paradoxical Sudden Calm!
        paradox_resp = client.post(
            "/api/v1/checkins",
            data=json.dumps(
                {
                    "id": "ci-day-6",
                    "date": "2026-11-16",
                    "dayLabel": "CN",
                    "time": "21:00",
                    "mood": 9,
                    "moodLabel": "Bình yên lạ thường",
                    "stress": 1,
                    "stressLabel": "Rất nhẹ",
                    "sleepHours": 8.0,
                    "sleepQuality": "refreshed",
                    "note": "Mình đã thu xếp xong hết mọi việc và tặng lại đồ đạc, cảm ơn mọi người vì tất cả.",
                }
            ),
            content_type="application/json",
            **hdr,
        ).json()
        self.assertTrue(paradox_resp["riskSnapshot"]["paradoxicalCalmDetected"])

        # Test stats/overview & WHO/CBT insightData on normal student
        stats_resp = client.get("/api/v1/stats/overview", HTTP_X_MINDCARE_CLIENT_ID="stu-demo-minh-nguyen")
        self.assertEqual(stats_resp.status_code, 200)
        self.assertGreaterEqual(len(stats_resp.json()["dailySeries"]), 5)

        chat_rag = client.post(
            "/api/v1/chat/stream",
            data=json.dumps({"message": "Dạo này mình hơi khó ngủ và áp lực đồ án tốt nghiệp"}),
            content_type="application/json",
            HTTP_X_MINDCARE_CLIENT_ID="stu-demo-minh-nguyen",
        )
        rag_frames = _parse_sse_frames(chat_rag)
        self.assertIsNotNone(rag_frames[0]["insightData"])
        self.assertIn("referenceTitle", rag_frames[0]["insightData"])

        # Test management command `run_risk_watchers` for Silent Dropout (>48h) & Cliffhanger (>10m)
        prof = get_or_init_profile("cold-longitudinal-student")
        RiskStateSnapshot.objects.filter(profile=prof).update(
            last_activity_at=timezone.now() - timedelta(hours=52),
            silent_dropout_flagged=False,
            last_high_chat_score=0.72,
            last_user_chat_at=timezone.now() - timedelta(minutes=15),
            cliffhanger_flagged=False,
        )
        call_command("run_risk_watchers")
        snap_refreshed = RiskStateSnapshot.objects.get(profile=prof)
        self.assertTrue(snap_refreshed.silent_dropout_flagged)
        self.assertTrue(snap_refreshed.cliffhanger_flagged)


class ApiContractCompletenessTests(TestCase):
    def test_all_api_contract_v2_endpoints_and_camelcase_keys(self):
        client = Client()
        hdr = {"HTTP_X_MINDCARE_CLIENT_ID": "stu-demo-minh-nguyen"}

        # 1. /health & /risk/state
        health = client.get("/api/v1/health").json()
        self.assertEqual(health["status"], "ok")
        self.assertIn("geminiConfigured", health)
        self.assertIn("model", health)

        risk = client.get("/api/v1/risk/state", **hdr).json()
        for key in (
            "rInst",
            "rEff",
            "currentTier",
            "floorTier",
            "floorExpiresAt",
            "channelScores",
            "attribution",
            "paradoxicalCalmDetected",
            "adaptiveScreeningPrompt",
            "updatedAt",
        ):
            self.assertIn(key, risk)

        # 2. /consent GET & PUT
        c_get = client.get("/api/v1/consent", **hdr).json()
        self.assertIn("allowAiCheckinPersonalize", c_get)
        c_put = client.put(
            "/api/v1/consent",
            data=json.dumps(
                {
                    "allowAiCheckinPersonalize": True,
                    "allowAiJournalContext": True,
                    "allowAutoCrisisSos": True,
                    "shareCoarseLocationOnSos": False,
                    "sosFeatureEnabled": True,
                }
            ),
            content_type="application/json",
            **hdr,
        ).json()
        self.assertTrue(c_put["allowAutoCrisisSos"])

        # 3. /contacts GET, POST, PUT, DELETE
        contacts = client.get("/api/v1/contacts", **hdr).json()
        self.assertGreaterEqual(len(contacts), 2)
        c_new = client.post(
            "/api/v1/contacts",
            data=json.dumps({"id": "tc-99", "name": "Lê Đào Minh Nhật", "relationship": "Bạn cùng nhóm", "phone": "0905111222"}),
            content_type="application/json",
            **hdr,
        ).json()
        self.assertEqual(c_new["id"], "tc-99")
        self.assertTrue(c_new["neverShareJournal"])

        c_upd = client.put(
            "/api/v1/contacts/tc-99",
            data=json.dumps({"shareLocation": True, "neverShareJournal": False}),
            content_type="application/json",
            **hdr,
        ).json()
        self.assertTrue(c_upd["shareLocation"])
        self.assertTrue(c_upd["neverShareJournal"])

        c_del = client.delete("/api/v1/contacts/tc-99", **hdr).json()
        self.assertTrue(c_del["deleted"])

        # 4. /screening/scales/{scale_type}, /screening/submit, /screening/safety-plan
        for sc in ("PHQ2_GAD2", "PHQ9", "GAD7"):
            sc_resp = client.get(f"/api/v1/screening/scales/{sc}", **hdr)
            self.assertEqual(sc_resp.status_code, 200)
            self.assertEqual(sc_resp.json()["scaleType"], sc)

        sub_micro = client.post(
            "/api/v1/screening/submit",
            data=json.dumps({"scaleType": "PHQ2_GAD2", "answers": {"phq1": 2, "phq2": 2, "gad1": 1, "gad2": 1}}),
            content_type="application/json",
            **hdr,
        ).json()
        self.assertEqual(sub_micro["recommendFollowUpScale"], "PHQ9")

        sp_get = client.get("/api/v1/screening/safety-plan", **hdr).json()
        self.assertIn("warningSigns", sp_get)
        sp_put = client.put(
            "/api/v1/screening/safety-plan",
            data=json.dumps(
                {
                    "warningSigns": ["Mất ngủ sau 1h sáng"],
                    "internalCoping": ["Thở 4-7-8"],
                    "safePlacesAndDistractions": ["Thư viện VKU"],
                    "trustedPeople": ["Nguyễn Thị Lan - 0905123456"],
                    "professionalContacts": ["096 306 1414", "115", "111"],
                    "safeEnvironmentSteps": ["Cất vật sắc nhọn"],
                }
            ),
            content_type="application/json",
            **hdr,
        ).json()
        self.assertEqual(sp_put["safePlacesAndDistractions"], ["Thư viện VKU"])

