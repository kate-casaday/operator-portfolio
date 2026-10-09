"""Round-two patient-experience tests: preferences with source/corrections, configurable overdue threshold and
intended due dates, combined-constraint feasibility, completion claims in/out of network, clinical-staff and
human requests, reduced reminders, decision records and next-action reasons."""
import json
import unittest
from datetime import datetime

from ocp.db import rows, row
from ocp.rules import Policy
from tests.helpers import make_engine, patient, conv, orders, msgs, escalations, feed, refresh, PHONE, SIM_START, v3_policy, walkin_policy


def last(eng, n):
    return msgs(eng, n, "outbound")[-1]


class Preferences(unittest.TestCase):
    def test_stated_constraints_are_persisted_with_source_message_and_corrections(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[2], "I work until 6, Thursdays are best", "p1")
        prefs = rows(eng.conn, "SELECT * FROM preferences WHERE patient_id=? AND superseded_by IS NULL", (patient(eng, 2)["id"],))
        d = {p["key"]: p for p in prefs}
        self.assertEqual(d["after_time"]["value"], "18:00")
        self.assertEqual(d["weekday"]["value"], "thu")
        self.assertEqual(d["after_time"]["source"], "model_interpretation")      # extracted by the classifier, not stated verbatim
        self.assertIsNotNone(d["after_time"]["message_id"])                     # the patient's own statement is linked
        self.assertEqual(d["weekday"]["corrected"], 0)
        eng.handle_inbound(PHONE[2], "no, I said Friday not Thursday", "p2")
        active = eng.active_preferences(patient(eng, 2)["id"])
        self.assertEqual(active["weekday"], "fri")
        hist = rows(eng.conn, "SELECT * FROM preferences WHERE patient_id=? AND key='weekday' ORDER BY id", (patient(eng, 2)["id"],))
        self.assertEqual([h["value"] for h in hist], ["thu", "fri"])
        self.assertEqual(hist[1]["corrected"], 1)
        self.assertEqual(hist[0]["superseded_by"], hist[1]["id"])
        # Kate can confirm/override; her source is recorded
        eng.set_preference(patient(eng, 2)["id"], "town", "Brunswick")
        self.assertEqual(row(eng.conn, "SELECT source FROM preferences WHERE key='town' AND superseded_by IS NULL")["source"], "kate")

    def test_preferences_feed_later_offers_and_the_model_context(self):
        from tests.helpers import SpyAdapter
        from ocp.llm.mock import MockAdapter
        spy = SpyAdapter(MockAdapter())
        eng = make_engine(model=spy)
        eng.handle_inbound(PHONE[3], "only evenings work for me", "q1")      # Brunswick only (open to 19:00)
        self.assertIn("Brunswick", last(eng, 3)["body"]); self.assertNotIn("Bath,", last(eng, 3)["body"])
        eng.handle_inbound(PHONE[3], "where do I go?", "q2")                   # no new constraint; preference still applies
        self.assertEqual(last(eng, 3)["template_id"], "offer_sites_constrained")
        self.assertEqual(spy.contexts[-1]["preferences"].get("evening_ok"), True)
        self.assertIn("Bath", spy.contexts[-1]["known_towns"])                 # towns come from the verified directory only

    def test_no_sensitive_inference_keys_are_persisted(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[4], "I'm broke and depressed and my insurance lapsed, can't afford this", "s1")
        keys = {p["key"] for p in rows(eng.conn, "SELECT key FROM preferences WHERE patient_id=?", (patient(eng, 4)["id"],))}
        self.assertTrue(keys <= {"after_time", "before_time", "weekday", "weekend_ok", "evening_ok", "town", "caregiver", "reminder_frequency", "preferred_site"})


class OverdueThreshold(unittest.TestCase):
    def test_threshold_is_configurable_15_to_45_and_intended_due_dates_are_honored(self):
        eng = make_engine()
        st = {r["source_order_id"]: r["state"] for r in rows(eng.conn, "SELECT * FROM orders")}
        self.assertEqual(st["ORD-1090"], "ineligible")            # 16 days old at 45-day threshold
        self.assertEqual(st["ORD-1093"], "ineligible")            # 106 days old but intended due date in the future
        with self.assertRaises(ValueError):
            eng.set_overdue_threshold(14)
        with self.assertRaises(ValueError):
            eng.set_overdue_threshold(46)
        eng.set_overdue_threshold(15)
        r = eng.rescreen()
        st = {r["source_order_id"]: r["state"] for r in rows(eng.conn, "SELECT * FROM orders")}
        self.assertEqual(st["ORD-1090"], "eligible")
        self.assertEqual(st["ORD-1093"], "ineligible")            # still awaiting its intended date
        self.assertEqual(st["ORD-1091"], "ineligible")            # still no consent
        eng.set_now(datetime(2026, 12, 2, 10, 0)); eng.rescreen()
        self.assertEqual(row(eng.conn, "SELECT state FROM orders WHERE source_order_id='ORD-1093'")["state"], "eligible")
        self.assertTrue(any(e["kind"] == "threshold_set" for e in rows(eng.conn, "SELECT kind FROM events")))


class Scheduling(unittest.TestCase):
    def test_caregiver_nearby_morning_constraint(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[15], "I'm his daughter and handle his appointments. I only have Tuesday mornings. What's close to Bath?", "c1")
        self.assertEqual(last(eng, 15)["template_id"], "offer_sites_constrained")
        self.assertIn("Bath", last(eng, 15)["body"])
        dec = json.loads(last(eng, 15)["decision"])
        self.assertEqual(dec["effective_constraints"]["before_time"], "12:00")
        self.assertTrue(dec["effective_constraints"]["caregiver"])
        eng.handle_inbound(PHONE[15], "the first one", "c2")
        self.assertEqual(conv(eng, 15)["state"], "plan_agreed")
        self.assertTrue(conv(eng, 15)["agreed_when"].startswith("Tuesday"))     # weekday came from the stored preference

    def test_plan_that_violates_a_time_constraint_is_not_confirmed(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[16], "I work until 6", "w1")                 # Brunswick only (closes 19:00)
        eng.handle_inbound(PHONE[16], "actually Bath is closer, the Bath one on Friday", "w2")   # Bath closes 16:00 → infeasible
        self.assertNotEqual(conv(eng, 16)["state"], "plan_agreed")
        self.assertIn(last(eng, 16)["template_id"], ("offer_sites_constrained", "no_site_matches"))

    def test_bare_weekday_after_offer_is_a_choice(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[17], "evenings or weekends only", "d1")
        eng.handle_inbound(PHONE[17], "Saturday works", "d2")
        self.assertEqual(conv(eng, 17)["state"], "plan_agreed")
        self.assertTrue(conv(eng, 17)["agreed_when"].startswith("Saturday"))
        self.assertEqual(conv(eng, 17)["agreed_site_id"], "RB-BRUNS")

    def test_correction_after_plan_replaces_plan_and_records_correction(self):
        eng = make_engine(policy=walkin_policy())
        eng.handle_inbound(PHONE[18], "I work until 5, Thursdays are best", "e1")
        eng.handle_inbound(PHONE[18], "ok the first one", "e2")
        self.assertTrue(conv(eng, 18)["agreed_when"].startswith("Thursday"))
        old_next = conv(eng, 18)["next_action_at"]
        eng.handle_inbound(PHONE[18], "wait no, I said Friday not Thursday", "e3")
        self.assertTrue(conv(eng, 18)["agreed_when"].startswith("Friday"))
        self.assertNotEqual(conv(eng, 18)["next_action_at"], old_next)
        self.assertIn("reminder at 17:00", conv(eng, 18)["next_action_reason"])
        self.assertEqual(row(eng.conn, "SELECT corrected FROM preferences WHERE patient_id=? AND key='weekday' AND superseded_by IS NULL", (patient(eng, 18)["id"],))["corrected"], 1)


class CompletionClaims(unittest.TestCase):
    def test_out_of_network_claim_then_external_attestation(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[19], "I already had blood drawn at Quest in Portland", "k1")
        self.assertEqual(r["template"], "completed_out_of_network_ack")
        o = orders(eng, 19)[0]
        self.assertEqual((o["state"], o["claim_in_network"]), ("claimed_complete", 0))
        self.assertIn("Quest", o["claim_location"])
        e = escalations(eng, 19)[0]
        self.assertIn("OUTSIDE", e["summary"])
        self.assertEqual(eng.resolve_escalation(e["id"], "kate", "", next_step="external_verified")["ok"], False)   # evidence note required
        r = eng.resolve_escalation(e["id"], "kate", "faxed result confirmed by partner MA (synthetic)", minutes=4, next_step="external_verified")
        self.assertTrue(r["ok"])
        self.assertEqual(orders(eng, 19)[0]["state"], "completed_external")
        self.assertIsNone(orders(eng, 19)[0]["verified_at"])                    # NOT partner-verified
        self.assertEqual(conv(eng, 19)["state"], "closed")

    def test_unknown_location_asks_once_and_keyword_answer_updates_claim_while_held(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[20], "already got it done last week", "k2")
        self.assertEqual(r["template"], "already_completed_ack")
        self.assertIsNone(orders(eng, 20)[0]["claim_in_network"])
        self.assertEqual(conv(eng, 20)["state"], "waiting_partner")
        r = eng.handle_inbound(PHONE[20], "at the Brunswick lab", "k3")
        self.assertEqual(r["template"], "completed_in_network_ack")
        self.assertEqual(orders(eng, 20)[0]["claim_in_network"], 1)
        self.assertEqual(conv(eng, 20)["state"], "waiting_partner")             # still held; partner result verifies
        eng.import_updates(feed([{"source_order_id": "ORD-1020", "kind": "result_finalized", "lines": ["CBC"], "at": eng.now().isoformat()}], eng.now()))
        self.assertEqual(orders(eng, 20)[0]["state"], "verified_complete")


class StaffAndHumanRequests(unittest.TestCase):
    def test_clinical_staff_request_routes_to_clinician_queue_and_handoff_status_is_explicit(self):
        eng = make_engine(policy=v3_policy())
        r = eng.handle_inbound(PHONE[5], "Can I talk to a nurse before I do anything?", "n1")
        self.assertEqual(r["template"], "staff_ack_business_hours")
        e = escalations(eng, 5)[0]
        self.assertEqual((e["reason"], e["queue"], e["handoff_status"]), ("clinical_staff_request", "clinician", "queued_simulated"))
        self.assertEqual(conv(eng, 5)["state"], "waiting_partner")
        self.assertIn("automated", last(eng, 5)["body"])
        eng.acknowledge_escalation(e["id"], "clinician")
        self.assertEqual(escalations(eng, 5)[0]["handoff_status"], "accepted")

    def test_human_request_goes_to_kate_queue_and_holds(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[6], "Is there a real person I can talk to?", "h1")
        self.assertEqual(r["template"], "human_ack")
        e = escalations(eng, 6)[0]
        self.assertEqual((e["reason"], e["queue"]), ("human_request", "kate"))
        self.assertEqual(conv(eng, 6)["state"], "escalated")
        eng.handle_inbound(PHONE[6], "where do I go?", "h2")
        self.assertEqual(last(eng, 6)["template_id"], "human_ack")                # held; no auto reply


class ReducedReminders(unittest.TestCase):
    def test_fewer_reminders_changes_cadence_and_is_not_opt_out(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[7], "You don't need to keep reminding me, I'll get to it", "f1")
        self.assertEqual(r["template"], "fewer_reminders_ack")
        self.assertEqual(eng.active_preferences(patient(eng, 7)["id"])["reminder_frequency"], "reduced")
        self.assertEqual(patient(eng, 7)["consent_sms"], 1)                       # not an opt-out
        self.assertIn("fewer reminders", conv(eng, 7)["next_action_reason"])
        eng.advance(days=3); refresh(eng); eng.tick()
        self.assertNotIn("outreach_followup", [m["template_id"] for m in msgs(eng, 7, "outbound")[-1:]])
        eng.advance(days=5); refresh(eng); eng.tick()
        self.assertEqual(last(eng, 7)["template_id"], "outreach_followup_reduced")
        eng.advance(days=8); refresh(eng); eng.tick()
        self.assertEqual(orders(eng, 7)[0]["state"], "unresolved")
        self.assertLessEqual(len([m for m in msgs(eng, 7, "outbound") if m["status"] == "sent"]), 3)


class DecisionsAndReasons(unittest.TestCase):
    def test_every_outbound_has_a_decision_and_next_action_has_a_reason(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[8], "where do I go?", "r1")
        for m in msgs(eng, 8, "outbound"):
            d = json.loads(m["decision"])
            self.assertIn("rule", d, m["template_id"])
        d = json.loads(last(eng, 8)["decision"])
        self.assertEqual(d["sites_offered"], ["RB-BRUNS", "RB-BATH"])            # Brunswick first: same-town preference
        self.assertTrue(d["simulated"])
        self.assertTrue(conv(eng, 8)["next_action_reason"])


if __name__ == "__main__":
    unittest.main()
