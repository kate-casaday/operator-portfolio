"""Version 4: clinical handoff through the partner's portal (link / relay / queue), emergency wording, preparation answers
from approved text, the operational resolver with an independent reviewer, the unclear menu, geo (zip, out of area,
consent-based location share), the shadow planner, and the v3 residuals Codex left open (V3-5 timing, V3-7 realized
variant, the interior-time false refusal).  Everything synthetic; no credentials."""
from __future__ import annotations

import json
import unittest
from datetime import datetime

from ocp.db import rows, row
from ocp.llm.base import ModelResult, ProviderError
from ocp.llm.composer import fact_check, FactComposer, ComposeRequest
from ocp.llm.resolver import RulesResolver, RulesReviewer, ResolveCase, Resolution, Verdict, Reviewer, Resolver
from ocp.portal import SimulatedPortal
from ocp.rules import Policy, prescreen_inbound
from ocp.server import ops_payload, conversation_payload
from tests.helpers import make_engine, patient, conv, orders, msgs, escalations, feed, refresh, FixedAdapter, v3_policy
from ocp.scenarios import P as PHONE


def last(eng, n):
    return msgs(eng, n, "outbound")[-1]


def events(eng, n, kind):
    return [json.loads(e["detail"]) for e in rows(eng.conn, "SELECT detail FROM events WHERE conversation_id=? AND kind=? ORDER BY id", (conv(eng, n)["id"], kind))]


class V4_Emergency(unittest.TestCase):
    def test_emergency_wording_is_code_decided_and_sends_911_text_through_a_pause(self):
        eng = make_engine(model=FixedAdapter(intent="willing", confidence=0.9))       # the model would say "willing"; code wins
        eng.pause("hold")
        r = eng.handle_inbound(PHONE[1], "I have chest pain and I can't breathe", "em1")
        self.assertEqual((r["intent"], r["template"], r["routing"]), ("emergency", "emergency_ack", "emergency"))
        m = last(eng, 1)
        self.assertEqual((m["status"], m["kind"], m["composer"]), ("sent", "safety", "template"))      # sent during the pause; never composed
        self.assertIn("911", m["body"]); self.assertIn("Urgent Care - Brunswick", m["body"])          # nearest verified urgent care to Bath
        e = escalations(eng, 1)[0]
        self.assertEqual((e["reason"], e["queue"], e["priority"]), ("emergency_wording", "clinician", "emergency"))
        self.assertEqual(conv(eng, 1)["state"], "waiting_partner")
        self.assertEqual(row(eng.conn, "SELECT COUNT(*) n FROM model_calls")["n"], 0)                # no model consulted

    def test_emergency_partner_item_is_optional_and_stop_still_wins(self):
        eng = make_engine(policy=Policy(emergency_notify_partner=False))
        eng.handle_inbound(PHONE[2], "call 911 I passed out", "em2")
        self.assertEqual(last(eng, 2)["template_id"], "emergency_ack"); self.assertEqual(escalations(eng, 2), [])
        eng.handle_inbound(PHONE[2], "STOP", "em3")
        self.assertEqual(conv(eng, 2)["state"], "closed")
        self.assertIsNone(prescreen_inbound("STOP", Policy()).menu_digit)
        self.assertEqual(prescreen_inbound("chest pains since this morning", Policy()).hard_intent, "emergency")


class V4_ClinicalHandoff(unittest.TestCase):
    def test_portal_link_is_the_default_no_queue_item_quiet_then_one_followup(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[4], "do I still need this test?", "cl1")
        self.assertEqual(r["template"], "clinical_portal_link"); self.assertEqual(escalations(eng, 4), [])
        m = last(eng, 4)
        self.assertIn("mychart", m["body"].lower()); self.assertIn("207-555-0100", m["body"]); self.assertEqual(m["composer"], "template")
        c = conv(eng, 4)
        self.assertEqual((c["state"], c["next_action"]), ("engaged", "clinical_followup"))
        self.assertEqual(events(eng, 4, "clinical_portal_referral")[0]["mode"], "portal_link")
        eng.advance(days=7, hours=1); refresh(eng); eng.tick()
        self.assertEqual(last(eng, 4)["template_id"], "clinical_followup_after_referral")
        self.assertEqual(conv(eng, 4)["next_action"], "followup")
        # then the patient picks up where they were
        r = eng.handle_inbound(PHONE[4], "ok, where do I go?", "cl2")
        self.assertIn(r["template"], ("offer_sites", "offer_sites_nearby"))

    def test_staff_request_and_clinical_wording_at_the_spend_cap_use_the_portal(self):
        eng = make_engine(model=FixedAdapter(intent="needs_location", confidence=0.9), policy=Policy(max_spend_usd_per_day=0.01))
        eng.model.simulated = False
        wall = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        eng.conn.execute("INSERT INTO model_calls(at,conversation_id,adapter,model,simulated,input_tokens,output_tokens,latency_ms,outcome,purpose,cost_usd,wall_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (wall, conv(eng, 1)["id"], "anthropic", "claude-opus-5", 0, 1000, 100, 1.0, "ok", "classify", 0.02, wall))
        eng.conn.commit()
        r = eng.handle_inbound(PHONE[1], "Can I talk to a nurse about my results?", "cap1")
        self.assertEqual((r["routing"], r["template"]), ("clinical_keywords", "staff_portal_link"))
        self.assertEqual(escalations(eng, 1), [])                                                     # the portal IS the clinical path
        self.assertEqual(row(eng.conn, "SELECT COUNT(*) n FROM model_calls")["n"], 1)               # no paid call

    def test_without_an_approved_portal_the_queue_is_used_only_when_the_partner_opted_in(self):
        eng = make_engine()
        eng.directory.patient_portal = {}
        r = eng.handle_inbound(PHONE[5], "do I still need this test?", "np1")
        self.assertTrue(r["template"].startswith("clinical_ack"))                                       # synthetic partner opted in (accepts_queue)
        self.assertEqual(escalations(eng, 5)[0]["queue"], "clinician")
        # V4-3: no portal and no opt-in → clinic phone only, no clinical promise, one configuration item for Kate
        eng2 = make_engine()
        eng2.directory.patient_portal = {}; eng2.directory.clinician_contact["accepts_queue"] = False
        self.assertEqual(eng2.clinical_route(), "none")
        r = eng2.handle_inbound(PHONE[5], "do I still need this test?", "np2")
        self.assertEqual(r["template"], "clinical_call_instructions")
        self.assertNotIn("logged", last(eng2, 5)["body"]); self.assertIn("207-555-0100", last(eng2, 5)["body"])
        es = escalations(eng2, 5)
        self.assertEqual([e["reason"] for e in es], ["clinical_route_missing"]); self.assertEqual(es[0]["queue"], "kate")
        eng2.handle_inbound(PHONE[5], "can I talk to a nurse?", "np3")
        self.assertEqual(len(escalations(eng2, 5)), 1)                                                   # once per conversation
        # clinician_queue policy without the opt-in is also "none"
        eng3 = make_engine(policy=v3_policy())
        eng3.directory.clinician_contact["accepts_queue"] = False
        r = eng3.handle_inbound(PHONE[5], "do I still need this test?", "np4")
        self.assertEqual(r["template"], "clinical_call_instructions")

    def test_clinician_queue_mode_is_version_3(self):
        eng = make_engine(policy=v3_policy())
        eng.handle_inbound(PHONE[6], "do I still need this test?", "cq1")
        self.assertEqual(escalations(eng, 6)[0]["queue"], "clinician"); self.assertEqual(conv(eng, 6)["state"], "waiting_partner")

    def test_prep_question_is_answered_from_approved_text_verbatim_and_never_for_a_line_without_it(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[2], "Do I need to fast before this?", "pr1")          # P-02: lipid panel, approved prep text
        self.assertEqual(r["template"], "prep_answer"); self.assertEqual(escalations(eng, 2), [])
        self.assertIn("nothing but water for 9 to 12 hours", last(eng, 2)["body"]); self.assertEqual(last(eng, 2)["composer"], "template")
        self.assertEqual(conv(eng, 2)["state"], "engaged"); self.assertEqual(conv(eng, 2)["next_action"], "followup")   # scheduling continues
        eng2 = make_engine()
        del eng2.catalog.tests["TSH"]["prep_instruction"]                                           # P-04: no approved prep text
        r = eng2.handle_inbound(PHONE[4], "Do I need to fast before this?", "pr2")
        self.assertEqual(r["template"], "clinical_portal_link")
        # a medication question that mentions timing is NOT a prep question
        eng3 = make_engine()
        r = eng3.handle_inbound(PHONE[2], "Should I stop my blood thinner before the draw?", "pr3")
        self.assertEqual(r["template"], "clinical_portal_link")

    def test_portal_relay_sends_the_patients_own_words_verbatim(self):
        eng = make_engine(policy=Policy(clinical_handoff="portal_relay"))
        r = eng.handle_inbound(PHONE[3], "Should I stop my blood thinner before the draw?", "rl1")
        self.assertEqual(r["template"], "clinical_relay_offer")
        self.assertTrue(conv(eng, 3)["relay_pending"])
        text = "Hi Dr. Nguyen, I take warfarin 5mg. Do I hold it before the blood test? Thanks, Carla"
        r = eng.handle_inbound(PHONE[3], text, "rl2")
        self.assertEqual((r["intent"], r["template"]), ("relay_sent", "clinical_relay_sent"))
        pm = rows(eng.conn, "SELECT * FROM portal_messages")[0]
        self.assertEqual((pm["body"], pm["status"], pm["adapter"]), (text, "sent_simulated", "simulated"))
        self.assertEqual(eng.portal.sent[0]["body"], text); self.assertIn("ORD-1003", pm["subject"])
        self.assertIsNone(conv(eng, 3)["relay_pending"]); self.assertEqual(escalations(eng, 3), [])
        self.assertEqual(row(eng.conn, "SELECT COUNT(*) n FROM model_calls WHERE purpose='compose' AND simulated=0")["n"], 0)   # nothing model-written
        # declining sends nothing; a failed portal opens a Kate item with the message preserved
        eng2 = make_engine(policy=Policy(clinical_handoff="portal_relay"))
        eng2.handle_inbound(PHONE[3], "do I still need this?", "rl3")
        r = eng2.handle_inbound(PHONE[3], "never mind", "rl4")
        self.assertEqual(r["template"], "clinical_relay_cancelled"); self.assertEqual(rows(eng2.conn, "SELECT * FROM portal_messages"), [])
        eng3 = make_engine(policy=Policy(clinical_handoff="portal_relay"))
        eng3.portal = SimulatedPortal(fail_times=1)
        eng3.handle_inbound(PHONE[3], "do I still need this?", "rl5")
        r = eng3.handle_inbound(PHONE[3], "Dr. Nguyen, is this still needed?", "rl6")
        self.assertEqual(r["template"], "human_ack"); self.assertEqual(rows(eng3.conn, "SELECT status FROM portal_messages")[0]["status"], "failed")
        self.assertEqual(escalations(eng3, 3)[0]["reason"], "human_request")

    def test_emergency_wording_inside_a_relay_message_is_still_an_emergency(self):
        eng = make_engine(policy=Policy(clinical_handoff="portal_relay"))
        eng.handle_inbound(PHONE[3], "do I still need this?", "re1")
        r = eng.handle_inbound(PHONE[3], "Dr. Nguyen I am having chest pain right now", "re2")
        self.assertEqual(r["template"], "emergency_ack"); self.assertEqual(rows(eng.conn, "SELECT * FROM portal_messages"), [])


class V4_Resolver(unittest.TestCase):
    def test_sunday_only_gets_the_closest_alternative_not_a_person(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[16], "Sunday would be easiest for me", "rs1")
        self.assertEqual(r["template"], "offer_alternative"); self.assertEqual(escalations(eng, 16), [])
        body = last(eng, 16)["body"]
        self.assertIn("Sunday", body); self.assertIn("Sat 8am-12pm", body); self.assertIn("Brunswick", body)
        d = events(eng, 16, "resolver_decision")[0]
        self.assertTrue(d["executed"]); self.assertEqual(d["verdict"], "agree"); self.assertEqual(d["chosen"], "alt-1")
        self.assertEqual(fact_check(body, json.loads(last(eng, 16)["decision"])["fact_sheet"]), [])

    def test_transport_with_a_ride_program_is_resolved_without_a_person_and_a_stop_in_town_wins(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[3], "I don't have a car and can't get there", "rt1")            # Bath: no stop in town → ride program
        self.assertEqual(r["template"], "transport_resolved"); self.assertEqual(escalations(eng, 3), [])
        self.assertIn("207-555-0199", last(eng, 3)["body"]); self.assertEqual(conv(eng, 3)["state"], "engaged")
        r = eng.handle_inbound(PHONE[21], "no car out here, can't get to Bath", "rt2")             # Phippsburg: the mobile stop in town
        self.assertEqual(r["template"], "offer_mobile_stop")
        self.assertIn("Phippsburg", last(eng, 21)["body"]); self.assertIn("Thursday", last(eng, 21)["body"]); self.assertIn("9am-12pm", last(eng, 21)["body"])
        self.assertEqual(fact_check(last(eng, 21)["body"], json.loads(last(eng, 21)["decision"])["fact_sheet"]), [])

    def test_no_ride_program_and_no_stop_still_escalates_with_what_was_tried(self):
        eng = make_engine()
        del eng.directory._instructions["transport"]
        r = eng.handle_inbound(PHONE[3], "I don't have a car and can't get there", "rt3")
        self.assertEqual(r["template"], "transport_ack")
        e = escalations(eng, 3)[0]
        self.assertEqual(e["reason"], "unresolved_barrier"); self.assertIn("Resolver tried", e["summary"])

    def test_shadow_mode_records_and_never_acts_off_skips(self):
        eng = make_engine(policy=Policy(resolver_mode="shadow"))
        r = eng.handle_inbound(PHONE[16], "Sunday would be easiest for me", "sh1")
        self.assertEqual(r["template"], "no_site_matches"); self.assertEqual(escalations(eng, 16)[0]["reason"], "unresolved_barrier")
        d = events(eng, 16, "resolver_decision")[0]
        self.assertTrue(d["shadow"]); self.assertFalse(d["executed"]); self.assertEqual(d["chosen"], "alt-1")
        eng2 = make_engine(policy=Policy(resolver_mode="off"))
        eng2.handle_inbound(PHONE[16], "Sunday would be easiest for me", "sh2")
        self.assertEqual(events(eng2, 16, "resolver_decision"), [])

    def test_reviewer_disagreement_goes_to_a_person_with_both_rationales(self):
        class Contrarian(Reviewer):
            name = "contrarian"
            def review(self, case, chosen):
                return Verdict(False, "the patient said Sunday only; Saturday is a stretch", reviewer=self.name)
        eng = make_engine()
        eng.reviewer = Contrarian()
        r = eng.handle_inbound(PHONE[16], "Sunday would be easiest for me", "rd1")
        self.assertEqual(r["template"], "handoff_generic")
        e = escalations(eng, 16)[0]
        self.assertEqual(e["reason"], "resolver_disagreement"); self.assertIn("Saturday is a stretch", e["summary"]); self.assertIn("relaxed", e["summary"])

    def test_rules_reviewer_refuses_an_option_outside_the_menu_and_clinical_wording(self):
        rv = RulesReviewer()
        case = ResolveCase(kind="schedule", patient_said=["Sundays only"], constraints={"weekday": "sun"},
                           options=[{"id": "alt-1", "kind": "sites_relaxed", "relaxed": ["weekday"], "sites": [{"id": "RB-BRUNS"}]}, {"id": "escalate", "kind": "escalate"}])
        self.assertFalse(rv.review(case, Resolution("alt-9", "made up")).agree)
        self.assertTrue(rv.review(case, Resolution("alt-1", "fits")).agree)
        case.patient_said = ["Sundays only, and my nurse said to hold my meds"]
        self.assertFalse(rv.review(case, Resolution("alt-1", "fits")).agree)

    def test_live_resolver_failure_falls_back_to_the_escalation_and_the_cap_skips_it(self):
        class Broken(Resolver):
            name = "anthropic"; simulated = False; model = "x"
            def resolve(self, case):
                raise ProviderError("down")
        eng = make_engine()
        eng.resolver = Broken()
        r = eng.handle_inbound(PHONE[16], "Sunday would be easiest for me", "rf1")
        self.assertEqual(r["template"], "no_site_matches"); self.assertEqual(escalations(eng, 16)[0]["reason"], "unresolved_barrier")
        self.assertTrue(events(eng, 16, "resolver_failed"))
        eng2 = make_engine(policy=Policy(max_spend_usd_per_day=0.0))
        eng2.resolver = Broken()
        eng2.handle_inbound(PHONE[16], "Sunday would be easiest for me", "rf2")
        self.assertTrue(events(eng2, 16, "resolver_skipped"))

    def test_two_unclear_replies_get_a_menu_and_a_digit_is_decided_by_code(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[10], "hmm", "u1"); eng.handle_inbound(PHONE[10], "??", "u2")
        self.assertEqual(last(eng, 10)["template_id"], "unclear_menu"); self.assertEqual(escalations(eng, 10), [])
        r = eng.handle_inbound(PHONE[10], "2", "u3")
        self.assertEqual(r["intent"], "already_completed"); self.assertEqual(last(eng, 10)["template_id"], "already_completed_ack")
        self.assertTrue(events(eng, 10, "menu_digit_decided"))
        eng2 = make_engine()
        for i, t in enumerate(("hmm", "??", "zzz")):
            eng2.handle_inbound(PHONE[10], t, "v%d" % i)
        self.assertEqual(last(eng2, 10)["template_id"], "handoff_generic"); self.assertEqual(escalations(eng2, 10)[0]["reason"], "model_low_confidence")
        eng3 = make_engine()
        r = eng3.handle_inbound(PHONE[10], "1", "w1")                                                 # a digit with no menu sent is just unclear
        self.assertEqual(r["template"], "unclear")


class V4_Geo(unittest.TestCase):
    def test_zip_geocodes_and_offers_nearest_with_distances(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[1], "I'm at my daughter's in 04578 this week, what's closest?", "g1")
        self.assertEqual(r["template"], "offer_sites_nearby")
        body = last(eng, 1)["body"]; d = json.loads(last(eng, 1)["decision"])
        self.assertIn("Wiscasset", body); self.assertEqual(d["sites_offered"][0], "RB-WISC")
        self.assertEqual(rows(eng.conn, "SELECT source, label FROM patient_locations")[0]["source"], "patient_zip")
        self.assertIsNotNone(d["fact_sheet"]["sites"][0]["distance_miles"]); self.assertLess(d["fact_sheet"]["sites"][0]["distance_miles"], 2)

    def test_far_zip_is_out_of_area_and_carries_the_distance_to_kate(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[1], "staying in 04401 this month, anything near there?", "g2")
        self.assertEqual(r["template"], "out_of_area_ack")
        e = escalations(eng, 1)[0]
        self.assertEqual(e["reason"], "out_of_area"); self.assertIn("miles away", e["summary"]); self.assertIn("Resolver tried", e["summary"])
        self.assertIn("miles", last(eng, 1)["body"])
        self.assertEqual(fact_check(last(eng, 1)["body"], json.loads(last(eng, 1)["decision"])["fact_sheet"]), [])

    def test_not_near_home_gets_the_consent_link_once_and_a_shared_location_is_used_once(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[1], "I'm not at home this week, what's closest to me?", "g3")
        self.assertEqual(r["template"], "location_link_offer")
        body = last(eng, 1)["body"]
        tok = rows(eng.conn, "SELECT * FROM location_links")[0]
        self.assertIn(tok["token"], body); self.assertEqual(fact_check(body, json.loads(last(eng, 1)["decision"])["fact_sheet"]), [])
        r = eng.record_shared_location(tok["token"], 44.0029, -69.6656)
        self.assertTrue(r["ok"]); self.assertEqual(r["nearest"][0], "RB-WISC"); self.assertEqual(last(eng, 1)["template_id"], "offer_sites_nearby")
        self.assertEqual(rows(eng.conn, "SELECT source FROM patient_locations")[0]["source"], "patient_shared")
        self.assertFalse(eng.record_shared_location(tok["token"], 44.0, -69.6)["ok"])                # single use
        self.assertFalse(eng.record_shared_location("nope", 44.0, -69.6)["ok"]); self.assertFalse(eng.record_shared_location(tok["token"], 999, 0)["ok"])
        eng2 = make_engine(policy=Policy(location_link_enabled=False))
        r = eng2.handle_inbound(PHONE[1], "I'm not at home this week, what's closest to me?", "g4")
        self.assertNotEqual(r["template"], "location_link_offer")

    def test_stored_location_expires_and_emergency_text_picks_urgent_care_by_distance(self):
        eng = make_engine(policy=Policy(location_ttl_days=1))
        eng.record_location(patient(eng, 1)["id"], 44.0329, -69.5187, "patient_town", "Damariscotta", "test")
        self.assertIsNotNone(eng.patient_point(patient(eng, 1)["id"]))
        eng.handle_inbound(PHONE[1], "I think I'm having a stroke", "g5")
        self.assertIn("Urgent Care - Damariscotta", last(eng, 1)["body"])
        eng.advance(days=2)
        self.assertIsNone(eng.patient_point(patient(eng, 1)["id"]))


class V4_PlannerShadow(unittest.TestCase):
    def test_proposed_action_is_recorded_and_compared_never_acted_on(self):
        eng = make_engine(model=FixedAdapter(intent="needs_location", confidence=0.9, proposed_action="clinical_handoff"))
        r = eng.handle_inbound(PHONE[1], "where do I go?", "ps1")
        self.assertIn(r["template"], ("offer_sites", "offer_sites_nearby"))                          # the rules acted, not the proposal
        d = events(eng, 1, "planner_shadow")[0]
        self.assertEqual((d["proposed"], d["agree"]), ("clinical_handoff", False))
        eng2 = make_engine(model=FixedAdapter(intent="needs_location", confidence=0.9, proposed_action="offer_sites"))
        eng2.handle_inbound(PHONE[1], "where do I go?", "ps2")
        self.assertTrue(events(eng2, 1, "planner_shadow")[0]["agree"])
        o = ops_payload(eng2)
        self.assertEqual(o["automation"]["planner_shadow"]["compared"], 1); self.assertEqual(o["automation"]["planner_shadow"]["agreement_pct"], 100.0)


class V4_Residuals(unittest.TestCase):
    """Codex's Sept 16 focused verification: V3-5 timing default, V3-7 realized variant, V3-1 interior-time false refusal."""
    def test_one_timing_rule_when_the_site_limit_is_later_than_the_catalog_default(self):
        eng = make_engine(tick=False)
        for s in eng.directory._sites:
            if s["id"] == "RB-BRUNS":
                s["service_hours"]["glucose_tolerance"]["latest_start"] = "11:00"
        eng.tick()
        body = [m for m in msgs(eng, 19, "outbound") if m["template_id"] == "outreach_initial"][0]
        fs = json.loads(body["decision"])["fact_sheet"]
        self.assertEqual(fs["sites"][0]["latest_start_text"], "must start by 10am")                   # the earlier of site and catalog, as in selection
        self.assertIn("10am", body["body"])
        req = eng._requirements(patient(eng, 19)["id"])
        self.assertEqual(eng._filter_sites({"after_time": "10:30"}, eng.now(), "Bath", req), [])

    def test_realized_variant_reflects_the_sent_text_not_the_fact_sheet(self):
        from tests.test_v3 import ScriptedComposer
        eng = make_engine(tick=False)
        eng.composer = ScriptedComposer([], fail=True)                                                  # provider failure → approved template
        eng.tick()
        v = json.loads(conv(eng, 1)["opener_variant"])
        self.assertEqual((v["writer"], v["sites_named"], v["disclosure_realized"], v["visit_date_present"]), ("template", 0, "none", False))
        self.assertEqual(v["sites_intended"], 2)

    def test_honest_interior_time_is_accepted_and_wrong_day_or_site_still_refused(self):
        eng = make_engine(tick=False)
        conv1 = row(eng.conn, "SELECT * FROM conversations WHERE patient_id=?", (patient(eng, 1)["id"],))
        fs = eng._fact_sheet(conv1, patient(eng, 1), "offer_sites", "reply", ["RB-BATH", "RB-BRUNS"], [], {}, {})
        fs["action_guide"] = {"question": True, "anchors": [["{site_names}"]]}
        ok = "Riverbend Health: Riverbend Lab - Bath is open Monday at 10am. Which works?"
        self.assertEqual(fact_check(ok, fs), [])
        self.assertTrue(fact_check("Riverbend Health: Riverbend Lab - Bath is open Sunday at 10am. Which works?", fs))
        self.assertTrue(fact_check("Riverbend Health: Riverbend Lab - Bath is open Monday at 6pm. Which works?", fs))
        # Codex's Sept 16 fresh bypasses that a vocabulary patch can catch (the boundary stays lexical; fact cards are the structural answer)
        for bad in ("Riverbend Health: Your testing is complimentary. Which works?", "Riverbend Health: Skip insulin before you come. Which works?",
                    "Riverbend Health: Your blood work shows diabetes. Which works?"):
            self.assertTrue(fact_check(bad, fs), bad)
        fs14 = eng._fact_sheet(row(eng.conn, "SELECT * FROM conversations WHERE patient_id=?", (patient(eng, 14)["id"],)), patient(eng, 14), "offer_sites", "reply", ["RB-BRUNS"], [], {}, {})
        fs14["action_guide"] = {"question": True, "anchors": [["{site_names}"]]}
        self.assertTrue(fact_check("Riverbend Health: Your toxicology screening is ready at Riverbend Lab - Brunswick. Which works?", fs14))


class V4_Surface(unittest.TestCase):
    def test_ops_payload_automation_section_and_conversation_payload_extras(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[4], "do I still need this?", "s1")
        eng.handle_inbound(PHONE[16], "Sunday would be easiest for me", "s2")
        o = ops_payload(eng)
        a = o["automation"]
        self.assertEqual(a["clinical_handoff"], "portal_link"); self.assertEqual(a["portal_referrals"], 1)
        self.assertEqual(a["resolver"]["executed"], 1); self.assertEqual(a["clinician_items_per_100_conversations"], 0.0)
        self.assertEqual(o["settings"]["resolver_mode"], "on")
        self.assertEqual([r["id"] for r in o["map"]["mobile_routes"]], ["MR-1"])
        c = conversation_payload(eng, conv(eng, 4)["id"])
        self.assertIn("portal_messages", c); self.assertIn("locations", c)

    def test_policy_rejects_an_unknown_handoff_mode(self):
        from ocp.rules import RuleViolation
        with self.assertRaises(RuleViolation):
            make_engine(policy=Policy(clinical_handoff="fax"))


if __name__ == "__main__":
    unittest.main()


class V4_Reconciliation(unittest.TestCase):
    """Codex's Sept 16 closing review of version 4: V4-1 hard constraints, V4-2 relay consent vs message, V4-3 partner opt-in,
    V4-4 one radius rule for every location source, V4-5 emergency recall / idioms, V4-6 planner families."""

    def test_v41_an_absolute_constraint_is_recorded_and_never_relaxed_even_at_execution(self):
        eng = make_engine()
        r = eng.handle_inbound(PHONE[2], "Sundays are the only day I can do, nothing else works", "h1")
        self.assertEqual(r["template"], "no_site_matches")                                             # not offer_alternative
        self.assertEqual(json.loads(eng.active_preferences(patient(eng, 2)["id"])["hard_constraints"]), ["weekday", "weekend_ok"])
        e = escalations(eng, 2)[0]
        self.assertEqual(e["reason"], "unresolved_barrier"); self.assertIn("no option fits; a person decides", e["summary"])
        # the hard constraint survives later turns: a follow-up "or evenings" still cannot relax Sunday
        eng2 = make_engine()
        eng2.handle_inbound(PHONE[2], "I must do Sunday", "h2")
        self.assertEqual(last(eng2, 2)["template_id"], "no_site_matches")
        # execution-time revalidation, independent of the menu: a hand-built relaxed option is refused
        opt = {"id": "alt-x", "kind": "sites_relaxed", "relaxed": ["weekday"], "kept": {}, "sites": [{"id": "RB-BRUNS"}]}
        self.assertEqual(eng2._execute_option(conv(eng2, 2), patient(eng2, 2), opt, {"weekday": "sun", "hard": ["weekday"]}, "conv%d:%%s:x" % conv(eng2, 2)["id"], {}), (None, None))
        self.assertTrue(events(eng2, 2, "resolver_option_refused"))
        # the rules reviewer refuses it too
        case = ResolveCase(kind="schedule", patient_said=["Sundays only"], constraints={"weekday": "sun", "hard": ["weekday"]},
                           options=[opt, {"id": "escalate", "kind": "escalate"}])
        self.assertFalse(RulesReviewer().review(case, Resolution("alt-x", "fits")).agree)
        # a soft preference is still relaxed
        eng3 = make_engine()
        self.assertEqual(eng3.handle_inbound(PHONE[16], "Sunday would be easiest for me", "h3")["template"], "offer_alternative")

    def test_v42_relay_acknowledgement_is_consent_not_the_message_and_sends_once(self):
        eng = make_engine(policy=Policy(clinical_handoff="portal_relay"))
        eng.handle_inbound(PHONE[3], "Should I stop warfarin before the draw?", "r1")
        r = eng.handle_inbound(PHONE[3], "yes please", "r2")
        self.assertEqual(r["template"], "clinical_relay_prompt"); self.assertEqual(rows(eng.conn, "SELECT * FROM portal_messages"), [])
        self.assertTrue(conv(eng, 3)["relay_pending"])                                                   # the offer stays open
        r = eng.handle_inbound(PHONE[3], "ok", "r3")
        self.assertEqual(r["template"], "clinical_relay_prompt")
        text = "Dr. Nguyen, should I stop my warfarin before the blood draw you ordered?"
        r = eng.handle_inbound(PHONE[3], text, "r4")
        self.assertEqual(r["template"], "clinical_relay_sent")
        pm = rows(eng.conn, "SELECT * FROM portal_messages")
        self.assertEqual(len(pm), 1); self.assertEqual(pm[0]["body"], text); self.assertEqual(pm[0]["status"], "sent_simulated")
        self.assertEqual(pm[0]["context_question"], "Should I stop warfarin before the draw?")             # the original question is kept
        self.assertEqual(len(eng.portal.sent), 1)
        # a third acknowledgement without a message ends the offer without sending
        eng2 = make_engine(policy=Policy(clinical_handoff="portal_relay"))
        eng2.handle_inbound(PHONE[3], "do I still need this?", "r5")
        for i, t in enumerate(("yes", "sure", "ok")):
            r = eng2.handle_inbound(PHONE[3], t, "r6%d" % i)
        self.assertEqual(r["template"], "clinical_relay_cancelled"); self.assertEqual(rows(eng2.conn, "SELECT * FROM portal_messages"), [])
        # NO after the prompt sends nothing
        eng3 = make_engine(policy=Policy(clinical_handoff="portal_relay"))
        eng3.handle_inbound(PHONE[3], "do I still need this?", "r7"); eng3.handle_inbound(PHONE[3], "yes", "r8")
        self.assertEqual(eng3.handle_inbound(PHONE[3], "NO", "r9")["template"], "clinical_relay_cancelled")

    def test_v42_portal_crash_after_acceptance_is_ambiguous_never_resent(self):
        class Crash(SimulatedPortal):
            def send_patient_message(self, *a, **k):
                res = super().send_patient_message(*a, **k)
                raise RuntimeError("process died after the portal accepted %s" % res.provider_message_id)
        eng = make_engine(policy=Policy(clinical_handoff="portal_relay"))
        eng.portal = Crash()
        eng.handle_inbound(PHONE[3], "do I still need this?", "c1")
        r = eng.handle_inbound(PHONE[3], "Dr. Nguyen, is this test still needed for me?", "c2")
        self.assertEqual(r["intent"], "processing_error")                                                 # the crash surfaced; nothing lied about "sent"
        self.assertEqual(rows(eng.conn, "SELECT status FROM portal_messages")[0]["status"], "sending")
        eng.tick()
        self.assertEqual(rows(eng.conn, "SELECT status FROM portal_messages")[0]["status"], "ambiguous")
        self.assertIn("portal_ambiguous", [e["reason"] for e in escalations(eng, 3)])
        self.assertEqual(len(eng.portal.sent), 1)                                                         # and never resent
        eng.tick(); self.assertEqual(len([e for e in escalations(eng, 3) if e["reason"] == "portal_ambiguous"]), 1)

    def test_v44_shared_device_location_obeys_the_same_radius_rule(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[1], "I'm not at home this week, what's closest to me?", "s1")
        tok = rows(eng.conn, "SELECT token FROM location_links")[0]["token"]
        r = eng.record_shared_location(tok, 44.8016, -68.7712)                                             # Bangor: 90+ miles from every site
        self.assertTrue(r["ok"]); self.assertEqual(r["offered"], "out_of_area_ack")
        e = escalations(eng, 1)[0]
        self.assertEqual(e["reason"], "out_of_area"); self.assertIn("patient_shared", e["summary"]); self.assertIn("miles away", e["summary"])
        self.assertNotIn("Wiscasset", last(eng, 1)["body"].split("closest is")[0])

    def test_v45_emergency_recall_and_idioms(self):
        p = Policy()
        for t in ("I can't catch my breath", "I want to die", "I took too many pills", "My face is drooping and my speech is slurred",
                  "my throat is swelling up", "I'm coughing up blood", "worst headache of my life"):
            self.assertEqual(prescreen_inbound(t, p).hard_intent, "emergency", t)
        for t in ("This bill is giving me chest pain", "the parking there is a heart attack", "it's not an emergency, just wondering",
                  "who is my emergency contact for the form?"):
            self.assertIsNone(prescreen_inbound(t, p).hard_intent, t)
        # no location at all: the urgent care line does not claim "nearest"
        eng = make_engine()
        eng.conn.execute("UPDATE patients SET home_town=NULL WHERE id=?", (patient(eng, 1)["id"],)); eng.conn.commit()
        eng.handle_inbound(PHONE[1], "I think I'm having a stroke", "e1")
        self.assertNotIn("nearest", last(eng, 1)["body"]); self.assertIn("not an emergency, urgent care:", last(eng, 1)["body"])

    def test_v46_planner_families_are_narrow_and_disposition_is_separate(self):
        from ocp.engine import Engine
        self.assertFalse(Engine._planner_agrees("transport_ack", "transport_resolved"))
        self.assertFalse(Engine._planner_agrees("unclear", "handoff_generic"))
        self.assertFalse(Engine._planner_agrees("clinical_handoff", "prep_answer"))
        self.assertFalse(Engine._planner_agrees("offer_sites", "offer_mobile_stop"))
        self.assertTrue(Engine._planner_agrees("offer_sites", "offer_sites_nearby"))
        self.assertTrue(Engine._planner_disposition_agrees("transport_ack", "transport_ack"))
        self.assertFalse(Engine._planner_disposition_agrees("transport_ack", "transport_resolved"))
        eng = make_engine(model=FixedAdapter(intent="needs_location", confidence=0.9, proposed_action="offer_sites"))
        eng.handle_inbound(PHONE[1], "where do I go?", "p1")
        d = events(eng, 1, "planner_shadow")[0]
        self.assertIn("disposition_agree", d)
        o = ops_payload(eng)
        self.assertEqual(o["automation"]["resolver"]["reviewer_label"], "rules (not a model)"); self.assertEqual(o["automation"]["clinical_route"], "portal")
