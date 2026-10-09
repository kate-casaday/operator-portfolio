"""Version 5: partner data schema + examples, fact cards and chart review (roles, quotation, conflicts, supersession),
longitudinal order state (replaced / modified / attended / patient-reported change), the referral lifecycle and
dashboard rules, booking outcomes tracked separately, feedback → evaluation cases → candidate changes, and the
assertions-audit invariants.  Everything synthetic; no credentials."""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime

from ocp.db import rows, row
from ocp import facts as F, referrals as R, scheduling as S, improve as I
from ocp.rules import Policy, prescreen_inbound
from ocp.server import ops_payload
from ocp.scheduling import SchedulingError
from tests.helpers import make_engine, patient, conv, orders, msgs, escalations, feed, refresh, v3_policy
from ocp.scenarios import P as PHONE, DATA

NOTES = json.load(open(os.path.join(DATA, "synthetic_notes.json")))


def last(eng, n):
    return msgs(eng, n, "outbound")[-1]


def with_notes(eng):
    F.import_notes(eng.conn, NOTES, eng.now()); eng.conn.commit()
    return eng


class V5_DataSpec(unittest.TestCase):
    def test_schema_and_examples_exist_and_examples_validate_against_required_fields(self):
        root = os.path.dirname(DATA)
        sch = json.load(open(os.path.join(DATA, "schema", "partner-data-v5.schema.json")))
        self.assertIn("capabilities", sch["required"])
        for name in ("orders-feed", "order-events", "clinical-notes", "escalation-routes"):
            ex = json.load(open(os.path.join(DATA, "examples", "%s.example.json" % name)))
            self.assertTrue(ex["synthetic"]); self.assertIn("capabilities", ex)
        self.assertTrue(os.path.exists(os.path.join(root, "docs", "partner-data-specification.md")))
        # record read never implies messaging or booking: the flags are separate and default off in the schema
        caps = sch["properties"]["capabilities"]["properties"]
        self.assertFalse(caps["messaging"]["default"]); self.assertFalse(caps["booking"]["default"]); self.assertFalse(caps["record_read"]["default"])


class V5_FactCards(unittest.TestCase):
    def test_rule_extraction_quotes_never_paraphrases_and_marks_gaps_and_conflicts(self):
        eng = with_notes(make_engine())
        # documented: the sentence with a rationale cue, verbatim
        ids = F.rule_extract_rationale(eng, patient(eng, 12)["id"])
        c = row(eng.conn, "SELECT * FROM fact_cards WHERE id=?", (ids[0],))
        self.assertEqual((c["class"], c["status"], c["extraction_method"]), ("documented", "proposed", "rule_extract"))
        self.assertIn(c["excerpt"], NOTES["clinical_notes"][0]["text"]); self.assertEqual(c["statement"], c["excerpt"])
        self.assertTrue(c["requires_clinical_review"])
        # mention without a reason → unresolved
        ids = F.rule_extract_rationale(eng, patient(eng, 2)["id"])
        self.assertEqual(row(eng.conn, "SELECT class, status FROM fact_cards WHERE id=?", (ids[0],))["class"], "unresolved")
        # no note at all → unresolved gap
        ids = F.rule_extract_rationale(eng, patient(eng, 4)["id"])
        self.assertIn("No note", row(eng.conn, "SELECT statement FROM fact_cards WHERE id=?", (ids[0],))["statement"])
        # two notes with different reasons → conflict group, both unresolved, flagged
        ids = F.rule_extract_rationale(eng, patient(eng, 3)["id"])
        cs = rows(eng.conn, "SELECT class, status, conflict_group FROM fact_cards WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
        self.assertEqual(len(cs), 2); self.assertTrue(all(x["class"] == "unresolved" and x["status"] == "flagged" for x in cs)); self.assertEqual(cs[0]["conflict_group"], cs[1]["conflict_group"])
        # a documented card cannot be proposed with an excerpt that is not verbatim
        with self.assertRaises(ValueError):
            F.propose(eng, patient(eng, 12)["id"], "order_rationale", "documented", "paraphrased reason", "NOTE-1201", "paraphrased reason", "Dr. Okafor", None, "manual_review", "kate")

    def test_roles_operator_cannot_approve_clinical_cards_reviewer_can_edit_must_be_verbatim(self):
        eng = with_notes(make_engine())
        cid = F.rule_extract_rationale(eng, patient(eng, 12)["id"])[0]
        r = F.review(eng, cid, "kate", "operator", "approve")
        self.assertFalse(r["ok"]); self.assertEqual(row(eng.conn, "SELECT status FROM fact_cards WHERE id=?", (cid,))["status"], "proposed")
        self.assertEqual(rows(eng.conn, "SELECT action FROM fact_reviews WHERE card_id=?", (cid,))[-1]["action"], "refuse")
        # the operator may flag it
        self.assertTrue(F.review(eng, cid, "kate", "operator", "flag", note="please confirm with Dr. Okafor")["ok"])
        # the clinical reviewer approves, logging chart-review minutes
        r = F.review(eng, cid, "dr-reviewer", "clinical_reviewer", "approve", note="matches the note", minutes=4.5)
        self.assertTrue(r["ok"])
        c = row(eng.conn, "SELECT * FROM fact_cards WHERE id=?", (cid,))
        self.assertEqual((c["status"], c["reviewer_role"]), ("approved", "clinical_reviewer")); self.assertIsNotNone(c["verified_at"])
        self.assertEqual(row(eng.conn, "SELECT SUM(minutes) m FROM human_time WHERE activity LIKE 'chart_review:%'")["m"], 4.5)
        # an edit that paraphrases is refused; an edit that is verbatim supersedes with a new version
        self.assertFalse(F.review(eng, cid, "dr-reviewer", "clinical_reviewer", "edit", statement="They wanted to see if the statin works")["ok"])
        r = F.review(eng, cid, "dr-reviewer", "clinical_reviewer", "edit", statement="Plan: recheck lipid panel in 8 weeks to assess response to statin.")
        self.assertTrue(r["ok"]); new = row(eng.conn, "SELECT * FROM fact_cards WHERE id=?", (r["card_id"],))
        self.assertEqual((new["version"], new["supersedes_id"], new["status"], new["class"]), (2, cid, "proposed", "documented"))
        self.assertEqual(row(eng.conn, "SELECT status, superseded_by FROM fact_cards WHERE id=?", (cid,))["superseded_by"], new["id"])
        # unresolved cards cannot be approved by anyone
        gap = F.rule_extract_rationale(eng, patient(eng, 4)["id"])[0]
        self.assertFalse(F.review(eng, gap, "dr-reviewer", "clinical_reviewer", "approve")["ok"])
        # administrative cards the operator may approve
        adm = F.propose(eng, patient(eng, 12)["id"], "care_team", "documented", "PCP: Dr. Nguyen", "feed", None, None, None, "partner_feed", "kate")
        self.assertTrue(F.review(eng, adm, "kate", "operator", "approve")["ok"])

    def test_rationale_reply_quotes_only_an_approved_card_else_honest_gap_and_referral(self):
        eng = with_notes(make_engine())
        r = eng.handle_inbound(PHONE[12], "Why did the doctor order this?", "r1")           # nothing approved yet
        self.assertEqual(r["template"], "rationale_unknown"); self.assertIn("won't guess", last(eng, 12)["body"])
        ref = rows(eng.conn, "SELECT * FROM referrals WHERE conversation_id=?", (conv(eng, 12)["id"],))[-1]
        self.assertEqual((ref["kind"], ref["delivery"]), ("portal_link", "unverified")); self.assertIn("order_rationale", ref["missing_data"])
        eng2 = with_notes(make_engine())
        cid = F.rule_extract_rationale(eng2, patient(eng2, 12)["id"])[0]
        F.review(eng2, cid, "dr-reviewer", "clinical_reviewer", "approve")
        r = eng2.handle_inbound(PHONE[12], "Why did the doctor order this?", "r2")
        self.assertEqual(r["template"], "rationale_documented")
        body = last(eng2, 12)["body"]
        self.assertIn("recheck lipid panel in 8 weeks to assess response to statin", body); self.assertIn("Dr. Okafor", body); self.assertIn("Jul 19", body)
        self.assertNotIn("because", body.split("says:")[0])                                  # attribution, not "your doctor ordered this because"
        self.assertEqual(rows(eng2.conn, "SELECT * FROM referrals WHERE conversation_id=?", (conv(eng2, 12)["id"],)), [])
        self.assertEqual(last(eng2, 12)["composer"], "template")                              # consequential detail is app-rendered


class V5_LongitudinalOrders(unittest.TestCase):
    def test_replaced_order_supersedes_and_completion_closes_the_right_order(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[12], "where do I go?", "l1")
        r = eng.import_updates(feed([{"event_id": "E1", "source_order_id": "ORD-1012", "kind": "replaced", "at": eng.now().isoformat(), "actor": "order system",
                                      "replacement": {"source_order_id": "ORD-1012B", "ordered_at": eng.now().isoformat(), "priority": "routine",
                                                      "lines": [{"test_code": "LIPID", "test_name": "Lipid panel"}, {"test_code": "A1C", "test_name": "Hemoglobin A1c"}]}}], eng.now()))
        old = row(eng.conn, "SELECT * FROM orders WHERE source_order_id='ORD-1012'"); new = row(eng.conn, "SELECT * FROM orders WHERE source_order_id='ORD-1012B'")
        self.assertEqual(old["state"], "replaced"); self.assertEqual(old["superseded_by"], new["id"]); self.assertIn(new["state"], ("eligible", "outreach_active"))
        self.assertEqual(conv(eng, 12)["state"], "engaged")                                    # the conversation continues on the replacement
        kinds = [e["kind"] for e in rows(eng.conn, "SELECT kind FROM order_events WHERE patient_id=? ORDER BY id", (patient(eng, 12)["id"],))]
        self.assertEqual(kinds[:1], ["imported"]); self.assertIn("replaced", kinds); self.assertIn("added", kinds)
        # a result for the OLD order does not close the new one; a result for the new one does
        eng.import_updates(feed([{"source_order_id": "ORD-1012", "kind": "result_finalized", "lines": ["LIPID"], "at": eng.now().isoformat()}], eng.now()))
        self.assertEqual(row(eng.conn, "SELECT state FROM orders WHERE source_order_id='ORD-1012B'")["state"] in ("eligible", "outreach_active"), True)
        eng.import_updates(feed([{"source_order_id": "ORD-1012B", "kind": "result_finalized", "lines": ["LIPID", "A1C"], "at": eng.now().isoformat()}], eng.now()))
        self.assertEqual(row(eng.conn, "SELECT state FROM orders WHERE source_order_id='ORD-1012B'")["state"], "verified_complete"); self.assertEqual(conv(eng, 12)["state"], "closed")
        # a replaced event without a replacement order is rejected, not half-applied
        eng2 = make_engine()
        eng2.import_updates(feed([{"event_id": "E2", "source_order_id": "ORD-1001", "kind": "replaced", "at": eng2.now().isoformat()}], eng2.now()))
        self.assertEqual(orders(eng2, 1)[0]["state"], "outreach_active"); self.assertTrue(rows(eng2.conn, "SELECT 1 FROM events WHERE kind='update_rejected'"))

    def test_modified_and_attended_events(self):
        eng = make_engine()
        eng.import_updates(feed([{"event_id": "M1", "source_order_id": "ORD-1011", "kind": "modified", "at": eng.now().isoformat(), "remove_lines": ["A1C"]}], eng.now()))
        self.assertEqual([l["status"] for l in rows(eng.conn, "SELECT status FROM order_lines WHERE order_id=? ORDER BY test_code", (orders(eng, 11)[0]["id"],))], ["cancelled", "outstanding"])
        eng.import_updates(feed([{"event_id": "M2", "source_order_id": "ORD-1011", "kind": "modified", "at": eng.now().isoformat(), "remove_lines": ["CBC"]}], eng.now()))
        self.assertEqual(orders(eng, 11)[0]["state"], "cancelled_by_partner")
        eng2 = make_engine()
        eng2.import_updates(feed([{"event_id": "A1", "source_order_id": "ORD-1001", "kind": "attended", "at": eng2.now().isoformat()}], eng2.now()))
        self.assertEqual(orders(eng2, 1)[0]["state"], "outreach_active")                       # attendance is not completion
        self.assertTrue(rows(eng2.conn, "SELECT 1 FROM events WHERE kind='attended_without_booking'"))

    def test_patient_reported_change_pauses_and_reconciles_never_changes_the_target(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[5], "My doctor said I don't need this anymore", "p1")
        self.assertEqual(r["template"], "plan_change_ack"); self.assertEqual(conv(eng, 5)["state"], "waiting_partner"); self.assertEqual(conv(eng, 5)["pause_reason"], "plan_change_reported")
        self.assertEqual(orders(eng, 5)[0]["state"], "outreach_active")                        # target unchanged
        self.assertEqual([l["status"] for l in rows(eng.conn, "SELECT status FROM order_lines WHERE order_id=?", (orders(eng, 5)[0]["id"],))], ["outstanding"])
        self.assertEqual(escalations(eng, 5)[0]["reason"], "plan_change_reconcile")
        self.assertTrue(rows(eng.conn, "SELECT 1 FROM order_events WHERE patient_id=? AND kind='patient_reported_change'", (patient(eng, 5)["id"],)))
        # no timer restarts it
        eng.advance(days=10); refresh(eng); eng.tick()
        self.assertEqual(conv(eng, 5)["state"], "waiting_partner"); self.assertFalse(any(m["template_id"] == "outreach_followup" and m["created_at"] > escalations(eng, 5)[0]["opened_at"] for m in msgs(eng, 5, "outbound")))
        # a provider event that keeps an order open resumes with approved wording; one that cancels closes
        eng.import_updates(feed([{"event_id": "R1", "source_order_id": "ORD-1005", "kind": "replaced", "at": eng.now().isoformat(),
                                  "replacement": {"source_order_id": "ORD-1005B", "ordered_at": eng.now().isoformat(), "priority": "routine", "lines": [{"test_code": "CMP", "test_name": "Comprehensive metabolic"}]}}], eng.now()))
        self.assertEqual(conv(eng, 5)["state"], "engaged"); self.assertEqual(last(eng, 5)["template_id"], "plan_change_confirmed"); self.assertIn("ORD-1005B", last(eng, 5)["body"])
        self.assertEqual(escalations(eng, 5)[0]["status"], "resolved")
        ref = rows(eng.conn, "SELECT * FROM referrals WHERE conversation_id=? AND kind='plan_change'", (conv(eng, 5)["id"],))[0]
        self.assertEqual((ref["state"], ref["outcome"]), ("resolved", "reconciled"))


class V5_Referrals(unittest.TestCase):
    def test_every_handoff_kind_creates_a_referral_with_the_right_state_and_delivery(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[4], "do I still need this test?", "h1")                     # portal link
        eng2 = make_engine(policy=Policy(clinical_handoff="portal_relay"))
        eng2.handle_inbound(PHONE[3], "do I still need this?", "h2"); eng2.handle_inbound(PHONE[3], "Dr. Nguyen, is this A1c still needed for me?", "h3")
        eng3 = make_engine(policy=v3_policy())
        eng3.handle_inbound(PHONE[6], "do I still need this?", "h4")                          # clinician queue
        eng4 = make_engine()
        eng4.handle_inbound(PHONE[1], "I have chest pain", "h5")                                # emergency
        eng5 = make_engine(); eng5.directory.patient_portal = {}; eng5.directory.clinician_contact["accepts_queue"] = False
        eng5.handle_inbound(PHONE[2], "do I still need this?", "h6")                          # route missing
        got = {}
        for e, n in ((eng, 4), (eng2, 3), (eng3, 6), (eng4, 1), (eng5, 2)):
            r = rows(e.conn, "SELECT kind, state, delivery, receiving_team, urgency, urgency_policy_status, escalation_id, portal_message_id FROM referrals WHERE conversation_id=?", (conv(e, n)["id"],))[-1]
            got[r["kind"]] = r
        self.assertEqual((got["portal_link"]["state"], got["portal_link"]["delivery"]), ("offered", "unverified"))
        self.assertEqual((got["portal_relay"]["state"], got["portal_relay"]["delivery"]), ("sent", "simulated")); self.assertIsNotNone(got["portal_relay"]["portal_message_id"])
        self.assertEqual((got["clinician_queue"]["state"], got["clinician_queue"]["delivery"]), ("queued", "simulated")); self.assertIsNotNone(got["clinician_queue"]["escalation_id"])
        self.assertEqual((got["emergency"]["state"], got["emergency"]["urgency"]), ("queued", "emergency"))
        self.assertEqual(got["route_missing"]["state"], "offered")
        self.assertTrue(all(r["urgency_policy_status"] == "pending_clinical_approval" for r in got.values()))
        # the clinician queue acknowledgement and resolution are evidence on the referral
        eid = got["clinician_queue"]["escalation_id"]
        eng3.acknowledge_escalation(eid, "clinician"); self.assertEqual(row(eng3.conn, "SELECT state FROM referrals WHERE escalation_id=?", (eid,))["state"], "acknowledged")
        eng3.resolve_escalation(eid, "clinician", "still needed; told the patient", minutes=3); self.assertEqual(row(eng3.conn, "SELECT state FROM referrals WHERE escalation_id=?", (eid,))["state"], "responded")

    def test_resolution_needs_basis_and_authority_and_sent_is_not_received(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[4], "do I still need this test?", "h1")
        rid = rows(eng.conn, "SELECT id FROM referrals")[0]["id"]
        self.assertFalse(R.resolve(eng, rid, "kate", "operator", "", "dashboard")["ok"])                      # no basis
        self.assertFalse(R.resolve(eng, rid, "kate", "operator", "looked fine", "dashboard")["ok"])          # operator, clinical, no evidence
        self.assertEqual(rows(eng.conn, "SELECT kind FROM referral_events WHERE referral_id=?", (rid,))[-1]["kind"], "refused")
        self.assertFalse(R.record_partner_evidence(eng, rid, "responded")["ok"])                                   # V5-8: a label is not evidence
        self.assertTrue(R.record_partner_evidence(eng, rid, "responded", detail={"source_ref": "PORTAL-THREAD-1", "summary": "still needed; proceed"})["ok"])
        self.assertTrue(R.resolve(eng, rid, "kate", "operator", "provider answered in the portal (evidence recorded)", "dashboard")["ok"])
        d = R.detail(eng, rid)
        self.assertEqual(d["referral"]["resolution_authority"], "operator:dashboard")
        # clinical reviewer may resolve without evidence, with a basis
        eng2 = make_engine(); eng2.handle_inbound(PHONE[4], "do I still need this test?", "h2")
        rid2 = rows(eng2.conn, "SELECT id FROM referrals")[0]["id"]
        self.assertTrue(R.resolve(eng2, rid2, "dr-reviewer", "clinical_reviewer", "reviewed the chart; no action needed", "designated reviewer")["ok"])
        # overdue + duplicate + filters
        eng3 = make_engine(); eng3.handle_inbound(PHONE[4], "do I still need this?", "d1"); eng3.handle_inbound(PHONE[4], "do I still need this test? asking again", "d2")
        p = R.payload(eng3); self.assertEqual(p["duplicates"], 1)
        eng3.advance(days=3); refresh(eng3); eng3.tick()
        p = R.payload(eng3, {"overdue": "1"}); self.assertGreaterEqual(p["overdue"], 1)
        self.assertEqual(R.payload(eng3, {"reason": "staff_request"})["referrals"], [])
        self.assertTrue(R.payload(eng3, {"missing": "order_rationale"})["referrals"])

    def test_emergency_notification_and_next_day_followup_are_separate_and_911_never_waits(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[1], "I have chest pain", "e1")
        self.assertEqual(last(eng, 1)["template_id"], "emergency_ack"); self.assertEqual(last(eng, 1)["status"], "sent")
        self.assertIn("call 911 now", last(eng, 1)["body"]); self.assertIn("not an emergency", last(eng, 1)["body"])   # urgent care named as non-emergency care
        ref = rows(eng.conn, "SELECT * FROM referrals WHERE kind='emergency'")[0]
        self.assertIsNotNone(ref["next_action_at"]); self.assertEqual([e["reason"] for e in escalations(eng, 1)], ["emergency_wording"])
        eng.advance(days=1, hours=1); refresh(eng); eng.tick()
        self.assertIn("emergency_followup", [e["reason"] for e in escalations(eng, 1)])
        self.assertTrue(any(e["kind"] == "followup_task" for e in rows(eng.conn, "SELECT kind FROM referral_events WHERE referral_id=?", (ref["id"],))))
        eng2 = make_engine(policy=Policy(emergency_notify_partner=False)); eng2.handle_inbound(PHONE[2], "I took too many pills", "e2")
        self.assertEqual(rows(eng2.conn, "SELECT state, delivery FROM referrals WHERE kind='emergency'")[0]["state"], "offered")


class V5_Booking(unittest.TestCase):
    def test_bookable_site_offers_slots_and_the_four_outcomes_stay_separate(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[2], "where do I go?", "b1")
        r = eng.handle_inbound(PHONE[2], "the first one, Thursday", "b2")
        self.assertEqual(r["template"], "offer_slots"); self.assertEqual(conv(eng, 2)["state"], "plan_agreed"); self.assertTrue(conv(eng, 2)["pending_slots"])
        self.assertNotIn("appointment", last(eng, 2)["body"].lower().replace("skip booking", ""))
        f = S.funnel(eng.conn); self.assertEqual((f["walk_in_plans_now"], f["conversations_with_a_confirmed_booking"]), (1, 0))
        r = eng.handle_inbound(PHONE[2], "1", "b3")
        self.assertEqual(r["template"], "booking_confirmed"); self.assertIn("Confirmation SIMBK-", last(eng, 2)["body"])
        b = S.active_booking(eng.conn, conv(eng, 2)["id"]); self.assertEqual((b["status"], b["adapter"]), ("booked", "simulated"))
        f = S.funnel(eng.conn); self.assertEqual((f["conversations_with_a_confirmed_booking"], f["conversations_attended"], f["patients_verified_complete"]), (1, 0, 0))
        eng.import_updates(feed([{"event_id": "AT1", "source_order_id": "ORD-1002", "kind": "attended", "at": eng.now().isoformat()}], eng.now()))
        self.assertEqual(row(eng.conn, "SELECT status FROM bookings WHERE id=?", (b["id"],))["status"], "attended"); self.assertEqual(orders(eng, 2)[0]["state"], "outreach_active")
        eng.import_updates(feed([{"source_order_id": "ORD-1002", "kind": "result_finalized", "lines": ["LIPID"], "at": eng.now().isoformat()}], eng.now()))
        f = S.funnel(eng.conn); self.assertEqual((f["conversations_attended"], f["patients_verified_complete"]), (1, 1))

    def test_walk_in_choice_move_and_failure_paths(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[2], "where do I go?", "w1"); eng.handle_inbound(PHONE[2], "the first one, Thursday", "w2")
        r = eng.handle_inbound(PHONE[2], "walk in", "w3")
        self.assertEqual(r["template"], "plan_confirmed"); self.assertIn("not a booked appointment", last(eng, 2)["body"]); self.assertIsNone(S.active_booking(eng.conn, conv(eng, 2)["id"]))
        eng2 = make_engine()
        eng2.handle_inbound(PHONE[2], "where do I go?", "m1"); eng2.handle_inbound(PHONE[2], "the first one, Thursday", "m2"); eng2.handle_inbound(PHONE[2], "2", "m3")
        self.assertTrue(S.active_booking(eng2.conn, conv(eng2, 2)["id"]))
        r = eng2.handle_inbound(PHONE[2], "MOVE", "m4")
        self.assertEqual(r["template"], "booking_moved"); self.assertIsNone(S.active_booking(eng2.conn, conv(eng2, 2)["id"])); self.assertEqual(conv(eng2, 2)["state"], "engaged")
        self.assertEqual(rows(eng2.conn, "SELECT status FROM bookings")[0]["status"], "cancelled")
        eng3 = make_engine(); eng3.scheduler.fail_times = 1
        eng3.handle_inbound(PHONE[2], "where do I go?", "f1"); eng3.handle_inbound(PHONE[2], "the first one, Thursday", "f2")
        r = eng3.handle_inbound(PHONE[2], "1", "f3")
        self.assertEqual(r["template"], "booking_failed_walkin"); self.assertEqual(rows(eng3.conn, "SELECT status FROM bookings")[0]["status"], "failed"); self.assertNotIn("Booked", last(eng3, 2)["body"])
        # a non-bookable site stays a walk-in plan
        eng4 = make_engine(policy=Policy(booking_enabled=False))
        eng4.handle_inbound(PHONE[2], "where do I go?", "n1"); r = eng4.handle_inbound(PHONE[2], "the first one, Thursday", "n2")
        self.assertEqual(r["template"], "plan_confirmed")


class V5_Improve(unittest.TestCase):
    def test_flag_any_target_make_case_review_export_run_and_candidate_lifecycle(self):
        eng = with_notes(make_engine())
        eng.handle_inbound(PHONE[12], "Why did the doctor order this?", "i1")
        m = last(eng, 12)
        f = I.flag(eng, "message", m["id"], "defect", "factual_support", "Should have quoted the note once approved", actor="kate")
        self.assertEqual((f["label"], f["status"]), ("defect", "open"))
        cid = F.rule_extract_rationale(eng, patient(eng, 12)["id"])[0]
        f2 = I.flag(eng, "fact_card", cid, "question", "factual_support", "Is the 8-week window still current?")
        rid = rows(eng.conn, "SELECT id FROM referrals")[0]["id"]
        f3 = I.flag(eng, "referral", rid, "preference", "routing", "Route rationale gaps to the ordering clinician's MA, not the clinician")
        self.assertEqual(len(rows(eng.conn, "SELECT id FROM feedback")), 3)
        with self.assertRaises(ValueError):
            I.flag(eng, "message", m["id"], "defect", "vibes", "x")
        c = I.case_from_feedback(eng, f["id"])
        self.assertEqual((c["status"], c["version"], c["target_kind"], c["category"]), ("draft", 1, "message", "factual_support"))
        self.assertEqual(I.run_case(c["case"])["status"], "needs_review")                       # a free-text expectation never passes by itself
        c = I.review_case(eng, c["id"], "kate", expect={"template_in": ["rationale_documented"]}, status="active")
        r = I.run_case(c["case"]); self.assertEqual(r["status"], "fail")                            # today's code (no approved card in a fresh engine)
        c2 = I.review_case(eng, c["id"], "kate", expect={"template_in": ["rationale_unknown"], "must_contain": ["won't guess"], "referral_kind": "portal_link"}, status="active")
        self.assertEqual((c2["version"], c2["supersedes_id"]), (2, c["id"])); self.assertEqual(row(eng.conn, "SELECT status FROM eval_cases WHERE id=?", (c["id"],))["status"], "retired")
        self.assertEqual(I.run_case(c2["case"])["status"], "pass")
        path = I.export_case(eng, c2["id"], out_dir="/tmp/ocp-v5-cases"); self.assertTrue(os.path.exists(path))
        ch = I.propose_change(eng, "quote approved rationale", "use the approved documented card in the rationale reply", [f["id"]], [c2["id"]])
        self.assertFalse(I.decide_change(eng, ch["id"], "approve", "kate")["ok"])                   # evaluate first
        res = I.evaluate_change(eng, ch["id"]); self.assertEqual(res["passed"], 1)
        self.assertTrue(I.decide_change(eng, ch["id"], "approve", "kate")["ok"])
        self.assertFalse(I.decide_change(eng, ch["id"], "release", "kate")["ok"])                   # tag required
        self.assertTrue(I.decide_change(eng, ch["id"], "release", "kate", tag="v5.0")["ok"])
        self.assertFalse(I.decide_change(eng, ch["id"], "rollback", "kate")["ok"])                  # reason required
        self.assertTrue(I.decide_change(eng, ch["id"], "rollback", "kate", note="regressed tone cases")["ok"])
        self.assertEqual([h["action"] for h in rows(eng.conn, "SELECT action FROM release_history ORDER BY id")], ["approve", "release", "rollback"])
        p = I.payload(eng); self.assertEqual(p["planner"]["mode"], "shadow"); self.assertIn("none exists", p["separation"]["training_data"])


class V5_Surface(unittest.TestCase):
    def test_ops_payload_has_v5_sections_and_measures(self):
        eng = with_notes(make_engine())
        eng.handle_inbound(PHONE[12], "Why did the doctor order this?", "s1")
        o = ops_payload(eng)
        v = o["v5"]
        self.assertEqual(v["referrals_total"], 1); self.assertIn("funnel", v); self.assertIn("completion_funnel", v["measures"]); self.assertIn("chart_review_minutes", v["measures"]["staff_workload"])
        self.assertEqual(v["scheduler"], {"adapter": "simulated", "simulated": True}); self.assertEqual(v["operator_role"], "operator")

    def test_seed_session_clears_v5_tables(self):
        from ocp.scenarios import load_orders_feed, SIM_START
        eng = with_notes(make_engine()); F.rule_extract_rationale(eng, patient(eng, 12)["id"]); eng.handle_inbound(PHONE[4], "do I still need this?", "z1")
        r = eng.seed_session(load_orders_feed(), SIM_START)
        self.assertEqual(r["interactive_conversations"], 25)
        for t in ("fact_cards", "referrals", "bookings", "order_events", "clinical_notes"):
            n = row(eng.conn, "SELECT COUNT(*) n FROM %s" % t)["n"]
            self.assertEqual(n, 0 if t != "order_events" else n)                                # order_events refill on import


if __name__ == "__main__":
    unittest.main()


class V5_Reconciliation(unittest.TestCase):
    """Codex's Sept 16 closing review of version 5: V5-1 capability enforcement, V5-2 source boundary, V5-3 importer state and
    chronology, V5-4 reconciliation binding and replacement re-screen, V5-5 emergency scoping, V5-6 slot bounds and stale
    offers, V5-7 cancel failure, V5-8 evidence and lifecycle, V5-9 improvement gates, V5-10 the v5 envelope imports."""

    def test_v51_capabilities_are_enforced_default_deny(self):
        eng = make_engine(tick=False)
        eng.directory.capabilities = {}                                                          # nothing granted
        eng.tick()
        self.assertEqual(rows(eng.conn, "SELECT COUNT(*) n FROM messages WHERE status='sent'")[0]["n"], 0)
        self.assertTrue(rows(eng.conn, "SELECT 1 FROM events WHERE kind='capability_denied'"))
        self.assertFalse(eng.import_notes(NOTES)["ok"])
        eng2 = make_engine(); eng2.directory.capabilities = dict(eng2.directory.capabilities, booking=False)
        eng2.handle_inbound(PHONE[2], "where do I go?", "c1"); r = eng2.handle_inbound(PHONE[2], "the first one, Thursday", "c2")
        self.assertEqual(r["template"], "plan_confirmed")                                        # walk-in plan, no slots
        eng3 = make_engine(policy=Policy(clinical_handoff="portal_relay")); eng3.directory.capabilities = dict(eng3.directory.capabilities, portal_relay=False)
        self.assertEqual(eng3.handle_inbound(PHONE[3], "do I still need this?", "c3")["template"], "clinical_portal_link")
        eng4 = make_engine(policy=v3_policy()); eng4.directory.capabilities = dict(eng4.directory.capabilities, clinician_queue={"accepted": False})
        self.assertEqual(eng4.clinical_route(), "none")
        # STOP confirmations still leave even without messaging (compliance)
        eng5 = make_engine(); eng5.directory.capabilities = dict(eng5.directory.capabilities, messaging=False)
        eng5.handle_inbound(PHONE[1], "STOP", "c4"); self.assertEqual(last(eng5, 1)["template_id"], "opt_out_confirm")

    def test_v52_documented_cards_need_a_real_patient_bound_source_and_clinical_content_needs_the_reviewer(self):
        eng = with_notes(make_engine())
        with self.assertRaises(ValueError):
            F.propose(eng, patient(eng, 12)["id"], "order_rationale", "documented", "The lipid panel proves your medication is safe.", "MISSING-NOTE", "The lipid panel proves your medication is safe.", "Dr. Fiction", "2026-07-01", "manual_review", "kate")
        with self.assertRaises(ValueError):                                                       # someone else's note
            F.propose(eng, patient(eng, 12)["id"], "order_rationale", "documented", "Plan: CBC to evaluate fatigue and check for anemia.", "NOTE-0101", "Plan: CBC to evaluate fatigue and check for anemia.", "x", None, "manual_review", "kate")
        cid = F.propose(eng, patient(eng, 12)["id"], "order_rationale", "documented", "Plan: recheck lipid panel in 8 weeks to assess response to statin.", "NOTE-1201",
                        "Plan: recheck lipid panel in 8 weeks to assess response to statin.", "Dr. Fiction", "2026-01-01", "manual_review", "kate")
        c = row(eng.conn, "SELECT author, authored_at FROM fact_cards WHERE id=?", (cid,))
        self.assertEqual(c["author"], "Dr. Okafor")                                                # attribution comes from the note, not the caller
        # clinical content in an administrative kind needs the reviewer
        adm = F.propose(eng, patient(eng, 12)["id"], "order_status_note", "documented", "Stop your statin because the result is normal.", "feed", None, None, None, "partner_feed", "kate")
        self.assertFalse(F.review(eng, adm, "kate", "operator", "approve")["ok"])
        # a source that disappears after approval is refused at use
        F.review(eng, cid, "dr-reviewer", "clinical_reviewer", "approve")
        eng.conn.execute("UPDATE clinical_notes SET text='(redacted)' WHERE note_id='NOTE-1201'"); eng.conn.commit()
        self.assertEqual(F.rationale_for_reply(eng, patient(eng, 12)["id"])["documented"], [])
        # a conflict is resolved explicitly: approving one side needs the reviewer + a note, and rejects the sibling with the reason
        eng2 = with_notes(make_engine()); ids = F.rule_extract_rationale(eng2, patient(eng2, 3)["id"])
        r = F.review(eng2, ids[0], "dr-reviewer", "clinical_reviewer", "edit", statement=row(eng2.conn, "SELECT excerpt FROM fact_cards WHERE id=?", (ids[0],))["excerpt"])
        new = r["card_id"]
        self.assertFalse(F.review(eng2, new, "dr-reviewer", "clinical_reviewer", "approve")["ok"])              # no resolution note
        self.assertTrue(F.review(eng2, new, "dr-reviewer", "clinical_reviewer", "approve", note="dermatology note is the later, more specific reason")["ok"])
        self.assertEqual(row(eng2.conn, "SELECT status, review_note FROM fact_cards WHERE id=?", (ids[1],))["status"], "rejected")

    def test_v53_importer_honours_feed_state_chronology_and_full_payload_ids(self):
        eng = make_engine(import_feed=False)
        feed_ = {"partner_id": "RIVERBEND", "generated_at": eng.now().isoformat(), "synthetic": True, "orders": [
            {"source_order_id": "X-1", "patient": {"source_patient_id": "PX-1", "display_name": "Can Celled", "phone": "+12075559001", "consent_sms": True, "home_town": "Bath"},
             "ordered_at": "2026-07-01T09:00:00", "priority": "routine", "state": "cancelled", "lines": [{"test_code": "CBC", "test_name": "Complete blood count"}]},
            {"source_order_id": "X-2", "patient": {"source_patient_id": "PX-2", "display_name": "Al Ready", "phone": "+12075559002", "consent_sms": True, "home_town": "Bath"},
             "ordered_at": "2026-07-01T09:00:00", "priority": "routine", "state": "open", "lines": [{"test_code": "CBC", "test_name": "Complete blood count", "status": "resulted"}]}]}
        eng.import_orders(feed_); eng.import_updates(feed([], eng.now())); eng.tick()
        self.assertEqual(row(eng.conn, "SELECT state FROM orders WHERE source_order_id='X-1'")["state"], "cancelled_by_partner")
        self.assertEqual(row(eng.conn, "SELECT state FROM orders WHERE source_order_id='X-2'")["state"], "verified_complete")
        self.assertEqual(rows(eng.conn, "SELECT COUNT(*) n FROM messages WHERE status='sent'")[0]["n"], 0)
        # chronology: an older modification does not overwrite a newer one; distinct same-time events do not collide
        eng2 = make_engine()
        eng2.import_updates(feed([{"source_order_id": "ORD-1011", "kind": "modified", "at": "2026-09-15T12:00:00", "remove_lines": ["A1C"]}], eng2.now()))
        eng2.import_updates(feed([{"source_order_id": "ORD-1011", "kind": "modified", "at": "2026-09-14T12:00:00", "add_lines": [{"test_code": "TSH", "test_name": "Thyroid"}]}], eng2.now()))
        self.assertEqual([l["test_code"] for l in rows(eng2.conn, "SELECT test_code FROM order_lines WHERE order_id=? AND status='outstanding'", (orders(eng2, 11)[0]["id"],))], ["CBC"])
        self.assertTrue(rows(eng2.conn, "SELECT 1 FROM events WHERE kind='update_out_of_order'"))
        eng3 = make_engine()
        eng3.import_updates(feed([{"source_order_id": "ORD-1011", "kind": "modified", "at": "2026-09-15T12:00:00", "remove_lines": ["A1C"]},
                                  {"source_order_id": "ORD-1011", "kind": "modified", "at": "2026-09-15T12:00:00", "remove_lines": ["CBC"]}], eng3.now()))
        self.assertEqual(orders(eng3, 11)[0]["state"], "cancelled_by_partner")

    def test_v54_only_a_resolving_event_after_the_report_resumes_and_a_replacement_is_rescreened(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[5], "My doctor said I don't need this anymore", "p1")
        eng.import_updates(feed([{"event_id": "OLD", "source_order_id": "ORD-1005", "kind": "attended", "at": "2026-09-13T09:00:00"}], eng.now()))
        self.assertEqual(conv(eng, 5)["state"], "waiting_partner"); self.assertTrue(rows(eng.conn, "SELECT 1 FROM events WHERE kind='reconciliation_not_resolved'"))
        eng.import_updates(feed([{"event_id": "OLD2", "source_order_id": "ORD-1005", "kind": "modified", "at": "2026-09-01T09:00:00", "add_lines": [{"test_code": "TSH", "test_name": "Thyroid"}]}], eng.now()))
        self.assertEqual(conv(eng, 5)["state"], "waiting_partner")                              # older than the report: recorded, not applied, not resolving
        eng.import_updates(feed([{"event_id": "NEW", "source_order_id": "ORD-1005", "kind": "cancelled", "at": eng.now().isoformat()}], eng.now()))
        self.assertEqual(conv(eng, 5)["state"], "closed")
        eng2 = make_engine()
        eng2.import_updates(feed([{"event_id": "R", "source_order_id": "ORD-1012", "kind": "replaced", "at": eng2.now().isoformat(),
                                   "replacement": {"source_order_id": "ORD-1012S", "ordered_at": eng2.now().isoformat(), "priority": "stat", "lines": [{"test_code": "LIPID", "test_name": "Lipid panel"}]}}], eng2.now()))
        self.assertEqual(row(eng2.conn, "SELECT state FROM orders WHERE source_order_id='ORD-1012S'")["state"], "ineligible")

    def test_v55_idioms_are_scoped_and_the_code_floor_rules_both_ways(self):
        p = Policy()
        self.assertEqual(prescreen_inbound("My emergency contact says I took too many pills", p).hard_intent, "emergency")
        self.assertEqual(prescreen_inbound("This is not an emergency but I have chest pain", p).hard_intent, "emergency")
        self.assertIsNone(prescreen_inbound("This bill is giving me chest pain", p).hard_intent)
        from tests.helpers import FixedAdapter as FA
        eng = make_engine(model=FA(intent="emergency", confidence=0.95))
        r = eng.handle_inbound(PHONE[1], "This bill is giving me chest pain", "i1")
        self.assertNotEqual(r["template"], "emergency_ack"); self.assertTrue(rows(eng.conn, "SELECT 1 FROM events WHERE kind='emergency_downgraded'"))
        eng2 = make_engine(model=FA(intent="unclear", confidence=0.2))
        self.assertEqual(eng2.handle_inbound(PHONE[1], "This is not an emergency but I have chest pain", "i2")["template"], "emergency_ack")

    def test_v56_slots_respect_bounds_and_a_stale_offer_cannot_be_booked(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[2], "I can only go Thursday after 6 pm", "s1")
        r = eng.handle_inbound(PHONE[2], "the first one, Thursday", "s2")
        # either fitting slots are offered (all at or after 6 pm) or, when none fit, the walk-in plan — never 8am/10am
        self.assertIn(r["template"], ("offer_slots", "offer_slot_one", "plan_confirmed"))
        for slot in json.loads(conv(eng, 2)["pending_slots"] or "{}").get("slots", []):
            self.assertGreaterEqual(slot[11:16], "18:00")
        self.assertNotIn("8am", last(eng, 2)["body"]); self.assertNotIn("10am", last(eng, 2)["body"])
        self.assertIn("constraints_applied", json.dumps(json.loads(last(eng, 2)["decision"])) if r["template"] != "plan_confirmed" else "constraints_applied")
        eng2 = make_engine()
        eng2.handle_inbound(PHONE[2], "where do I go?", "t1"); eng2.handle_inbound(PHONE[2], "the first one, Thursday", "t2")
        eng2.handle_inbound(PHONE[2], "flibbertigibbet", "t3"); eng2.handle_inbound(PHONE[2], "flibbertigibbet", "t4")
        self.assertIsNone(conv(eng2, 2)["pending_slots"]); self.assertTrue(rows(eng2.conn, "SELECT 1 FROM events WHERE kind='slot_offer_superseded'"))
        eng2.handle_inbound(PHONE[2], "1", "t5"); r = eng2.handle_inbound(PHONE[2], "1", "t6")
        self.assertNotEqual(r["template"], "booking_confirmed"); self.assertEqual(rows(eng2.conn, "SELECT COUNT(*) n FROM bookings")[0]["n"], 0)

    def test_v57_cancel_failure_keeps_the_booking_and_tells_the_truth(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[2], "where do I go?", "m1"); eng.handle_inbound(PHONE[2], "the first one, Thursday", "m2"); eng.handle_inbound(PHONE[2], "1", "m3")
        def broken(*a, **k):
            raise SchedulingError("partner unavailable")
        eng.scheduler.cancel = broken
        r = eng.handle_inbound(PHONE[2], "MOVE", "m4")
        self.assertEqual(r["template"], "booking_move_failed"); self.assertIn("still stands", last(eng, 2)["body"])
        b = S.active_booking(eng.conn, conv(eng, 2)["id"]); self.assertEqual(b["status"], "cancel_pending")
        self.assertIn("booking_change_failed", [e["reason"] for e in escalations(eng, 2)]); self.assertEqual(conv(eng, 2)["state"], "escalated")

    def test_v58_evidence_is_an_artifact_and_terminal_states_hold(self):
        eng = make_engine(); eng.handle_inbound(PHONE[4], "do I still need this test?", "e1")
        rid = rows(eng.conn, "SELECT id FROM referrals")[0]["id"]
        self.assertFalse(R.record_partner_evidence(eng, rid, "acknowledged", actor="kate")["ok"])
        self.assertFalse(R.record_partner_evidence(eng, rid, "responded", actor="kate", detail={"source_ref": "x"})["ok"])          # no summary
        self.assertTrue(R.record_partner_evidence(eng, rid, "responded", actor="kate", detail={"source_ref": "PORTAL-THREAD-9", "summary": "provider: still needed"})["ok"])
        self.assertTrue(R.resolve(eng, rid, "kate", "operator", "provider answered (thread 9)", "dashboard")["ok"])
        self.assertFalse(R.transition(eng, rid, "acknowledged", actor="partner"))                                                     # terminal holds
        r = row(eng.conn, "SELECT state, resolved_at FROM referrals WHERE id=?", (rid,)); self.assertEqual(r["state"], "resolved"); self.assertIsNotNone(r["resolved_at"])
        self.assertEqual(rows(eng.conn, "SELECT kind FROM referral_events WHERE referral_id=? ORDER BY id DESC LIMIT 1", (rid,))[0]["kind"], "late_event")
        eng2 = make_engine(); eng2.handle_inbound(PHONE[4], "do I still need this test?", "e2")
        rid2 = rows(eng2.conn, "SELECT id FROM referrals")[0]["id"]
        self.assertFalse(R.transition(eng2, rid2, "resolved", actor="kate")); self.assertEqual(row(eng2.conn, "SELECT state FROM referrals WHERE id=?", (rid2,))["state"], "offered")

    def test_v59_unchecked_expectations_never_pass_and_approval_needs_passing_reviewed_cases(self):
        r = I.run_case({"patient": "P-02", "expect": {"should_have_text": "impossible", "booking_state": "booked", "no_referral": True}})
        self.assertEqual(r["status"], "fail")                                                           # booking_state is checked now
        r = I.run_case({"patient": "P-02", "expect": {"should_have_text": "impossible", "no_referral": True}})
        self.assertEqual(r["status"], "needs_review")                                                   # a pending free-text check keeps it from passing
        self.assertEqual(I.run_case({"patient": "P-02", "expect": {"nonsense_key": 1}})["status"], "error")
        eng = make_engine()
        ch = I.propose_change(eng, "empty", "no cases", [], [])
        I.evaluate_change(eng, ch["id"]); self.assertFalse(I.decide_change(eng, ch["id"], "approve", "kate")["ok"])
        eng.handle_inbound(PHONE[4], "do I still need this?", "x1")
        f = I.flag(eng, "message", last(eng, 4)["id"], "defect", "routing", "should route to the nurse line")
        c = I.case_from_feedback(eng, f["id"])
        c = I.review_case(eng, c["id"], "kate", expect={"referral_kind": "clinician_queue"}, status="reviewed")       # fails on today's code
        ch2 = I.propose_change(eng, "route", "change the route", [f["id"]], [c["id"]])
        I.evaluate_change(eng, ch2["id"]); self.assertFalse(I.decide_change(eng, ch2["id"], "approve", "kate")["ok"])
        self.assertTrue(I.decide_change(eng, ch2["id"], "approve", "kate", note="exception: partner asked for the change before the case can pass")["ok"])
        # replay restores approved cards and provider events before the texts
        eng3 = with_notes(make_engine()); cid = F.rule_extract_rationale(eng3, patient(eng3, 12)["id"])[0]; F.review(eng3, cid, "dr", "clinical_reviewer", "approve")
        eng3.handle_inbound(PHONE[12], "Why did the doctor order this?", "x2")
        f3 = I.flag(eng3, "message", last(eng3, 12)["id"], "preference", "tone", "warmer")
        c3 = I.case_from_feedback(eng3, f3["id"]); self.assertTrue(c3["case"]["pre"]["approved_cards"])
        self.assertEqual(I.run_case(dict(c3["case"], expect={"template_in": ["rationale_documented"]}))["status"], "pass")

    def test_v510_the_v5_envelope_imports_end_to_end_and_unsupported_envelopes_are_rejected(self):
        from ocp.importer import FeedRejected
        eng = make_engine(import_feed=False)
        eng.set_now(datetime.fromisoformat("2026-09-16T07:00:00"))                                  # the examples are dated Sept 16
        ex = json.load(open(os.path.join(DATA, "examples", "orders-feed.example.json")))
        r = eng.import_orders(ex); self.assertEqual(r["orders_created"], 1)
        o = row(eng.conn, "SELECT * FROM orders WHERE source_order_id='ORD-1024'"); self.assertEqual(o["ordering_provider"], "Dr. Okafor"); self.assertIn("episode_team", o["care_team"])
        eng.set_now(datetime.fromisoformat("2026-09-20T07:00:00"))
        ev = json.load(open(os.path.join(DATA, "examples", "order-events.example.json")))
        r = eng.import_updates(ev)
        self.assertIn(("replaced"), [c for _, c in r["applied"]])
        self.assertEqual(row(eng.conn, "SELECT state FROM orders WHERE source_order_id='ORD-1024B'")["state"], "verified_complete")
        with self.assertRaises(FeedRejected):
            eng.import_updates({"partner_id": "RIVERBEND", "generated_at": eng.now().isoformat(), "somethingelse": []})
