"""Regression cases registered by Codex's round-two closing review (C1-C12, RC5-RC13) and their fixes."""
import json
import os
import tempfile
import unittest
from datetime import datetime

from ocp import cli
from ocp import feedback as fb
from ocp.db import rows, row, connect
from ocp.directory import Directory
from ocp.engine import Engine
from ocp.llm.mock import MockAdapter
from ocp.messaging.simulated import SimulatedMessaging
from ocp.rules import Policy
from ocp.scenarios import load_orders_feed, SIM_START, run_demo
from tests.helpers import make_engine, patient, conv, orders, msgs, escalations, feed, refresh, PHONE, DIRECTORY, v3_policy, walkin_policy


def last(eng, n):
    return msgs(eng, n, "outbound")[-1]


class C1_FreshSession(unittest.TestCase):
    def test_seed_from_empty_db_leaves_interactive_patients(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "fresh.sqlite")
        eng = cli.build_engine(path)
        self.assertEqual(row(eng.conn, "SELECT COUNT(*) n FROM patients")["n"], 0)
        r = eng.seed_session(load_orders_feed(), SIM_START)
        self.assertEqual(r["interactive_conversations"], 25)
        self.assertEqual(row(eng.conn, "SELECT COUNT(*) n FROM conversations WHERE state='outreach_sent'")["n"], 25)
        self.assertIsNone(eng.paused())
        # Kate can immediately play a patient through the same path as the webhook
        rr = eng.handle_inbound(PHONE[1], "where do I go?", None)
        self.assertEqual(rr["template"], "offer_sites")

    def test_fresh_session_after_full_demo_keeps_feedback(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "demo.sqlite")
        eng = cli.build_engine(path)
        run_demo(eng, quiet=True)
        self.assertEqual(row(eng.conn, "SELECT COUNT(*) n FROM conversations WHERE state IN ('outreach_sent','engaged')")["n"], 0)   # the finding
        m = row(eng.conn, "SELECT id FROM messages WHERE direction='outbound' AND status='sent' ORDER BY id LIMIT 1")
        f = fb.create(eng, m["id"], "question", "Why say the provider's name here?")
        r = eng.seed_session(load_orders_feed(), SIM_START)
        self.assertEqual(r["interactive_conversations"], 25)
        self.assertEqual(r["feedback_rows_kept"], 2)                                        # the v5 journey scenario flags one reply
        lst = fb.list_all(eng)
        self.assertEqual(lst[0]["id"], f["id"]); self.assertIn("archived session", lst[0]["display_name"])
        out = fb.export_task(eng, f["id"], out_dir=tempfile.mkdtemp())      # still exportable from its own evidence
        self.assertIn("Why say the provider", out["markdown"])

    def test_cli_seed_command(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "cli.sqlite")
        self.assertEqual(cli.main(["seed", "--db", path]), 0)
        eng = cli.build_engine(path)
        self.assertEqual(row(eng.conn, "SELECT COUNT(*) n FROM conversations WHERE state='outreach_sent'")["n"], 25)


class C2_ClinicalDuringHold(unittest.TestCase):
    def test_rc11_doctor_request_during_cost_hold_opens_clinician_item(self):
        eng = make_engine(policy=v3_policy())
        eng.handle_inbound(PHONE[1], "What is the copay?", "rc11a")
        self.assertEqual(escalations(eng, 1)[0]["queue"], "kate")
        r = eng.handle_inbound(PHONE[1], "Can I speak with my doctor first?", "rc11b")
        self.assertEqual(r["intent"], "request_clinical_staff")
        es = escalations(eng, 1)
        self.assertEqual([e["queue"] for e in es], ["kate", "clinician"])
        self.assertEqual(es[1]["reason"], "clinical_staff_request")
        self.assertIn(last(eng, 1)["template_id"], ("staff_ack_business_hours", "staff_ack_after_hours"))
        self.assertEqual(conv(eng, 1)["state"], "escalated")                    # the scheduling hold is kept
        eng.acknowledge_escalation(es[1]["id"], "clinician")
        self.assertEqual(escalations(eng, 1)[1]["handoff_status"], "accepted")

    def test_rc9_nurse_request_is_not_consumed_as_a_location_answer(self):
        eng = make_engine(policy=v3_policy())
        eng.handle_inbound(PHONE[2], "Already done last week", "rc9a")
        self.assertEqual(last(eng, 2)["template_id"], "already_completed_ack")
        eng.handle_inbound(PHONE[2], "Not at the Brunswick lab; at Quest", "rc9b")
        self.assertEqual(orders(eng, 2)[0]["claim_in_network"], 0)
        loc_before = orders(eng, 2)[0]["claim_location"]
        r = eng.handle_inbound(PHONE[2], "Can I talk to a nurse about Quest results?", "rc9c")
        self.assertEqual(r["intent"], "request_clinical_staff")
        self.assertEqual(orders(eng, 2)[0]["claim_location"], loc_before)           # unchanged
        self.assertTrue(any(e["queue"] == "clinician" for e in escalations(eng, 2)))
        self.assertEqual(conv(eng, 2)["state"], "waiting_partner")
        self.assertIsNone(eng.active_preferences(patient(eng, 2)["id"]).get("weekday"))   # "last week" is not a weekday

    def test_rc10_negated_lab_is_out_of_network(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[3], "Already done last month", "rc10a")
        r = eng.handle_inbound(PHONE[3], "Not at your lab", "rc10b")
        self.assertEqual(r["template"], "completed_out_of_network_ack")
        self.assertEqual(orders(eng, 3)[0]["claim_in_network"], 0)
        self.assertIsNone(eng.active_preferences(patient(eng, 3)["id"]).get("weekday"))

    def test_rc12_human_request_during_clinician_hold_opens_kate_item(self):
        eng = make_engine(policy=v3_policy())
        eng.handle_inbound(PHONE[4], "Can I speak with my nurse?", "rc12a")
        r = eng.handle_inbound(PHONE[4], "I need a real person to help with the ride", "rc12b")
        self.assertEqual(r["template"], "human_ack")
        self.assertEqual([e["queue"] for e in escalations(eng, 4)], ["clinician", "kate"])
        self.assertEqual(conv(eng, 4)["state"], "escalated")
        # a repeat is annotated, not duplicated
        eng.handle_inbound(PHONE[4], "hello? a real person please", "rc12c")
        self.assertEqual(len(escalations(eng, 4)), 2)

    def test_where_answer_only_when_the_question_is_pending(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[5], "How much does it cost?", "w1")           # Kate hold, no where-question
        n = len(escalations(eng, 5))
        r = eng.handle_inbound(PHONE[5], "I went to Quest", "w2")
        self.assertEqual(r["intent"], "held_for_human")
        self.assertIsNone(orders(eng, 5)[0]["claim_location"])
        self.assertEqual(len(escalations(eng, 5)), n)


class C3_Constraints(unittest.TestCase):
    def test_rc7_contradictory_bounds_ask_instead_of_planning(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[6], "Where can I go?", "rc7a")
        r = eng.handle_inbound(PHONE[6], "the first one Friday after 3pm before 9am", "rc7b")
        self.assertEqual(r["template"], "clarify_times")
        self.assertNotEqual(conv(eng, 6)["state"], "plan_agreed")

    def test_rc5_replacement_constraint_withdraws_the_old_one_and_the_plan(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[7], "I work until 6 on Fridays", "rc5a")
        eng.handle_inbound(PHONE[7], "the Brunswick one Friday", "rc5b")
        self.assertEqual(conv(eng, 7)["state"], "plan_agreed")
        r = eng.handle_inbound(PHONE[7], "Actually I no longer work evenings; mornings only now", "rc5c")
        act = eng.active_preferences(patient(eng, 7)["id"])
        self.assertEqual(act.get("before_time"), "12:00")
        self.assertNotIn("after_time", act)                                        # withdrawn by contradiction
        hist = rows(eng.conn, "SELECT * FROM preferences WHERE patient_id=? AND key='after_time'", (patient(eng, 7)["id"],))
        self.assertEqual(hist[0]["superseded_by"], -1)
        # Brunswick is open Friday mornings, so the Friday plan is still feasible and may stand; the decision says so
        c = conv(eng, 7)
        if c["state"] == "plan_agreed":
            self.assertEqual(c["agreed_site_id"], "RB-BRUNS")
            self.assertIn(r["template"], ("offer_sites_constrained", "offer_sites", "plan_confirmed"))
        else:
            self.assertIn(r["template"], ("offer_sites_constrained", "no_site_matches", "offer_sites"))

    def test_plan_that_new_constraints_rule_out_is_cleared(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[9], "where do I go?", "inv1")
        eng.handle_inbound(PHONE[9], "the Bath one on Friday", "inv2")            # Bath closes 16:00
        self.assertEqual(conv(eng, 9)["agreed_site_id"], "RB-BATH")
        r = eng.handle_inbound(PHONE[9], "actually I can only come after 5", "inv3")
        self.assertNotEqual(conv(eng, 9)["state"], "plan_agreed")
        self.assertIsNone(conv(eng, 9)["agreed_site_id"])
        self.assertIn("Brunswick", last(eng, 9)["body"])
        self.assertTrue(any(e["kind"] == "reminder_cancelled" for e in rows(eng.conn, "SELECT kind FROM events WHERE conversation_id=?", (conv(eng, 9)["id"],))))


class C4_C9_Feedback(unittest.TestCase):
    def test_delayed_flag_keeps_response_time_evidence_and_export_attaches_full_json(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "t.sqlite")
        eng = make_engine(db_path=path)
        eng.handle_inbound(PHONE[8], "I work until 6 on Thursdays", "c4a")
        offer = msgs(eng, 8, "outbound")[-1]
        eng.handle_inbound(PHONE[8], "the first one", "c4b")
        eng.handle_inbound(PHONE[8], "No, I meant Friday not Thursday", "c4c")
        eng.set_overdue_threshold(20)
        f = fb.create(eng, offer["id"], "defect", "Should have offered Brunswick only with a reason.")
        ev = f["evidence"]
        snap = ev["flagged_message"]["decision"]["response_time_snapshot"]
        self.assertEqual(snap["overdue_threshold_days"], 45)                        # response-time value
        self.assertEqual(ev["configuration"]["overdue_threshold_days"], 20)          # flag-time value
        self.assertIn("template_text", snap); self.assertIn("directory_content", snap)
        self.assertEqual(ev["software"]["ocp_version"], "0.3.0")
        self.assertTrue(any(p["key"] == "weekday" and p["value"] == "thu" for p in ev["preferences_history"]))
        eng.conn.close()
        conn = connect(path)
        eng2 = Engine(conn, Directory.load(DIRECTORY), MockAdapter(), SimulatedMessaging(), Policy())
        out = fb.export_task(eng2, f["id"], out_dir=os.path.join(d, "exports"))
        self.assertTrue(os.path.exists(out["evidence_path"]))
        full = json.load(open(out["evidence_path"]))
        self.assertEqual(full["evidence"]["flagged_message"]["id"], offer["id"])
        self.assertIn("interpretation", full)
        self.assertIn("software ocp 0.3.0", out["markdown"])
        self.assertIn("Constraint history", out["markdown"])
        self.assertIn("PHONE[8]", out["markdown"])                                   # stub resolved to the synthetic patient

    def test_status_lifecycle_requires_evidence(self):
        eng = make_engine()
        m = msgs(eng, 9, "outbound")[-1]
        f = fb.create(eng, m["id"], "preference", "warmer wording")
        with self.assertRaises(ValueError):
            fb.update_status(eng, f["id"], "linked")
        with self.assertRaises(ValueError):
            fb.update_status(eng, f["id"], "verified", verification_note="checked")   # no link yet
        fb.update_status(eng, f["id"], "linked", linked_ref="commit abc")
        with self.assertRaises(ValueError):
            fb.update_status(eng, f["id"], "verified")                              # no note
        fb.update_status(eng, f["id"], "verified", verification_note="tests/test_x.py")
        fb.update_status(eng, f["id"], "open")
        ev = rows(eng.conn, "SELECT detail FROM events WHERE kind='feedback_status' ORDER BY id DESC LIMIT 1")[0]
        self.assertTrue(json.loads(ev["detail"])["reopened"])


class C5_ContentDigests(unittest.TestCase):
    def test_link_text_change_with_same_timestamp_cancels_queued_offer(self):
        eng = make_engine()
        eng.pause("hold")
        eng.handle_inbound(PHONE[10], "where do I go?", "c5a")
        eng.directory._instructions["scheduling_link"]["text"] = "https://example.invalid/riverbend/NEW"
        eng.resume(); eng.tick()
        offer = [m for m in msgs(eng, 10, "outbound") if m["template_id"] == "offer_sites"][0]
        self.assertEqual(offer["status"], "cancelled")
        self.assertFalse(any("riverbend/schedule" in x["body"] for x in eng.messaging.sent if x["to"] == PHONE[10]))

    def test_unsigned_instruction_is_never_offered_and_queued_one_is_cancelled(self):
        eng = make_engine(policy=v3_policy())
        eng.pause("hold")
        eng.handle_inbound(PHONE[11], "no car, can't get there", "c5b")
        del eng.directory._instructions["transport"]["approved_by"]
        eng.resume(); eng.tick()
        m = [m for m in msgs(eng, 11, "outbound") if m["template_id"] == "transport_ack"][0]
        self.assertEqual(m["status"], "cancelled")
        self.assertIsNone(eng.directory.instruction("transport", eng.now()))
        eng.handle_inbound(PHONE[12], "no car, can't get there", "c5c")
        self.assertNotIn("free ride", last(eng, 12)["body"])


class C10_MonotonicAttempts(unittest.TestCase):
    def test_rc13_history_is_kept_and_cadence_is_reduced(self):
        eng = make_engine()
        for _ in range(2):
            eng.advance(days=3); refresh(eng); eng.tick()
        self.assertEqual(conv(eng, 13)["outreach_attempts"], 3)
        eng.handle_inbound(PHONE[13], "Please ease off the reminders, once a week is enough", "rc13")
        self.assertEqual(conv(eng, 13)["outreach_attempts"], 3)                      # not rewritten
        self.assertEqual(conv(eng, 13)["reduced_allowance"], 1)
        eng.advance(days=7); refresh(eng); eng.tick()
        self.assertEqual(last(eng, 13)["template_id"], "outreach_followup_reduced")
        self.assertEqual(conv(eng, 13)["outreach_attempts"], 4)
        eng.advance(days=7); refresh(eng); eng.tick()
        self.assertEqual(orders(eng, 13)[0]["state"], "unresolved")
        self.assertEqual(patient(eng, 13)["consent_sms"], 1)


if __name__ == "__main__":
    unittest.main()


class FV_PostVerification(unittest.TestCase):
    """Codex's focused-verification residuals FV1-FV5 (fixed AFTER the authorized closing process; unverified by Codex)."""

    def test_fv1_model_failure_during_hold_never_becomes_a_location_answer(self):
        from ocp.llm.mock import MockAdapter
        eng = make_engine(policy=v3_policy(), model=MockAdapter())
        eng.handle_inbound(PHONE[1], "Already done last month", "fv1a")
        eng.model.fail_times = 1
        r = eng.handle_inbound(PHONE[1], "Can I talk to a nurse about Quest results?", "fv1b")
        self.assertEqual(r["intent"], "request_clinical_staff")
        self.assertIsNone(orders(eng, 1)[0]["claim_location"])
        self.assertTrue(any(e["queue"] == "clinician" for e in escalations(eng, 1)))
        self.assertIn(last(eng, 1)["template_id"], ("staff_ack_business_hours", "staff_ack_after_hours"))

    def test_fv1_exhausted_budget_during_hold_still_routes_clinical_by_keywords(self):
        eng = make_engine(policy=v3_policy(max_model_calls_per_conversation=3))
        eng.handle_inbound(PHONE[2], "What is the copay?", "fv1c")
        eng.handle_inbound(PHONE[2], "thanks", "fv1d"); eng.handle_inbound(PHONE[2], "thanks again", "fv1e")
        self.assertEqual(conv(eng, 2)["model_calls"], 3)
        r = eng.handle_inbound(PHONE[2], "Can I speak with my doctor first?", "fv1f")
        self.assertEqual(r["intent"], "request_clinical_staff")
        self.assertEqual(conv(eng, 2)["model_calls"], 3)                              # no further model call
        self.assertTrue(any(e["queue"] == "clinician" for e in escalations(eng, 2)))
        kinds = [e["kind"] for e in rows(eng.conn, "SELECT kind FROM events WHERE conversation_id=?", (conv(eng, 2)["id"],))]
        self.assertIn("held_keyword_screen", kinds)

    def test_fv1_token_and_inbound_ceilings_gate_held_classification(self):
        eng = make_engine(policy=Policy(max_inbound_per_conversation=3))
        eng.handle_inbound(PHONE[3], "How much?", "fv1g")
        eng.handle_inbound(PHONE[3], "ok", "fv1h")
        calls = conv(eng, 3)["model_calls"]
        eng.handle_inbound(PHONE[3], "hello", "fv1i")
        self.assertEqual(conv(eng, 3)["model_calls"], calls)                           # ceiling respected while held

    def test_fv2_rc5_effective_constraints_never_contradict_and_rc7_follow_up_does_not_plan(self):
        eng = make_engine(policy=walkin_policy())
        eng.handle_inbound(PHONE[4], "I work until 6 on Fridays", "fv2a")
        eng.handle_inbound(PHONE[4], "the Brunswick one Friday", "fv2b")
        eng.handle_inbound(PHONE[4], "Actually I no longer work evenings; mornings only now", "fv2c")
        act = eng.active_preferences(patient(eng, 4)["id"])
        self.assertEqual(act.get("before_time"), "12:00")
        self.assertNotIn("after_time", act); self.assertNotIn("evening_ok", act)
        eng2 = make_engine(policy=walkin_policy())
        eng2.handle_inbound(PHONE[5], "Where can I go?", "fv2d")
        eng2.handle_inbound(PHONE[5], "the first one Friday after 3pm before 9am", "fv2e")
        act = eng2.active_preferences(patient(eng2, 5)["id"])
        self.assertNotIn("after_time", act); self.assertNotIn("before_time", act)      # contradictory bounds not persisted
        r = eng2.handle_inbound(PHONE[5], "the first one Friday", "fv2f")
        self.assertEqual(r["template"], "plan_confirmed")                              # now allowed: no stored contradiction
        eng3 = make_engine(policy=walkin_policy())
        eng3.conn.execute("INSERT INTO preferences(patient_id,key,value,source,created_at) VALUES(?,?,?,?,?)", (patient(eng3, 6)["id"], "after_time", "15:00", "kate", "2026-09-15T10:00:00"))
        eng3.conn.execute("INSERT INTO preferences(patient_id,key,value,source,created_at) VALUES(?,?,?,?,?)", (patient(eng3, 6)["id"], "before_time", "09:00", "kate", "2026-09-15T10:00:00"))
        eng3.conn.commit()
        eng3.handle_inbound(PHONE[6], "where do I go?", "fv2g")
        r = eng3.handle_inbound(PHONE[6], "the first one Friday", "fv2h")
        self.assertEqual(r["template"], "clarify_times")                               # stored empty window blocks planning

    def test_fv3_transport_message_keeps_its_day(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[7], "I can't drive anymore after my surgery, my son takes me on Sundays", "fv3")
        act = eng.active_preferences(patient(eng, 7)["id"])
        self.assertTrue(act.get("weekday") == "sun" or act.get("weekend_ok") is True)

    def test_fv4_routed_reserves_two_requests_and_prices_per_model(self):
        import importlib, sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        run_eval = importlib.import_module("eval.run_eval")
        g = run_eval.SpendGuard(1.0, 1, 200, "claude-opus-5")
        ok, why = g.allow(run_eval.PARTNER_CTX, "Where can I go?", requests_per_invocation=2)
        self.assertFalse(ok); self.assertIn("2 requests", why)
        g = run_eval.SpendGuard(1.0, 5, 200, "claude-opus-5")
        g.record_attempts([{"model": "claude-haiku-4-5", "input_tokens": 900, "output_tokens": 80, "cache_read_tokens": 10},
                           {"model": "claude-opus-5", "input_tokens": 900, "output_tokens": 80, "cache_read_tokens": 10}])
        self.assertAlmostEqual(g.spent, (900 * 1 + 10 * 0.10 + 80 * 5 + 900 * 5 + 10 * 0.5 + 80 * 25) / 1e6, places=6)

    def test_fv5_archived_feedback_not_shown_on_a_reused_conversation(self):
        from ocp.server import conversation_payload
        from ocp.scenarios import load_orders_feed, SIM_START
        eng = make_engine()
        m = msgs(eng, 8, "outbound")[-1]
        fb.create(eng, m["id"], "question", "why this wording?")
        eng.seed_session(load_orders_feed(), SIM_START)
        self.assertEqual(conversation_payload(eng, conv(eng, 8)["id"])["feedback"], [])
        self.assertEqual(len(fb.list_all(eng)), 1)

    def test_c4_snapshot_carries_full_directory_records_and_policy(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[9], "where do I go?", "c4r")
        d = json.loads(last(eng, 9)["decision"])["response_time_snapshot"]
        self.assertEqual(d["directory_records"]["sites"]["RB-BRUNS"]["verified_by"], "partner ops (synthetic)")
        self.assertEqual(d["directory_records"]["instructions"]["scheduling_link"]["approved_by"], "partner ops (synthetic)")
        self.assertEqual(d["policy"]["max_outbound_per_conversation"], 12)
