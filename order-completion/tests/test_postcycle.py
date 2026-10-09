"""Regression tests for the problems Codex's final verification (turn 4) found in the reconciliation.
These fixes were made AFTER the authorized four-stage cycle and have NOT been verified by Codex."""
import json
import os
import tempfile
import unittest
import urllib.error
import socket
from datetime import datetime
from unittest import mock

from ocp.db import rows, row, connect
from ocp.directory import Directory
from ocp.engine import Engine
from ocp.llm.mock import MockAdapter
from ocp.messaging.simulated import SimulatedMessaging
from ocp.messaging.base import MessagingError, AmbiguousSendError
from ocp.rules import Policy, add_business_hours
from tests.helpers import make_engine, patient, conv, orders, msgs, escalations, feed, refresh, PHONE, SIM_START, DIRECTORY, v3_policy


def sent(eng, n):
    return [m for m in msgs(eng, n, "outbound") if m["status"] == "sent"]


def new_order(pid_src, name, phone, oid, town="Bath"):
    return {"source_order_id": oid, "patient": {"source_patient_id": pid_src, "display_name": name, "phone": phone,
                                                "consent_sms": True, "home_town": town}, "ordered_at": "2026-07-01T09:00:00",
            "ordering_provider": "Dr. S", "priority": "routine", "lines": [{"test_code": "TSH", "test_name": "TSH"}]}


class N1_Reopen(unittest.TestCase):
    def test_reopened_conversation_actually_sends_new_outreach(self):
        eng = make_engine()
        eng.import_updates(feed([{"source_order_id": "ORD-1001", "kind": "result_finalized", "lines": ["CBC"], "at": eng.now().isoformat()}], eng.now()))
        n_before = len(sent(eng, 1))
        eng.import_orders({"partner_id": "RIVERBEND", "generated_at": eng.now().isoformat(), "orders": [new_order("P-01", "Alice Winslow", PHONE[1], "ORD-1001B")]})
        self.assertEqual(conv(eng, 1)["episode"], 2)
        eng.tick()
        self.assertEqual(len(sent(eng, 1)), n_before + 1)                       # a NEW message, not a dedupe hit
        self.assertEqual(sent(eng, 1)[-1]["template_id"], "outreach_initial")
        self.assertEqual(sent(eng, 1)[-1]["epoch"], conv(eng, 1)["epoch"])
        # follow-up cadence in the new episode is not blocked by episode-1 keys either
        eng.advance(days=3); refresh(eng); eng.tick()
        self.assertEqual(sent(eng, 1)[-1]["template_id"], "outreach_followup")

    def test_new_order_cannot_escape_an_open_clinician_item(self):
        eng = make_engine(policy=v3_policy())
        eng.handle_inbound(PHONE[4], "do I still need this test?", "c1")
        eng.import_updates(feed([{"source_order_id": "ORD-1004", "kind": "result_finalized", "lines": ["TSH"], "at": eng.now().isoformat()}], eng.now()))
        self.assertEqual(conv(eng, 4)["state"], "closed")
        self.assertEqual(escalations(eng, 4)[0]["status"], "open")
        eng.import_orders({"partner_id": "RIVERBEND", "generated_at": eng.now().isoformat(), "orders": [new_order("P-04", "Dan Pelletier", PHONE[4], "ORD-1004B", "Topsham")]})
        self.assertEqual(conv(eng, 4)["state"], "waiting_partner")
        self.assertIsNone(conv(eng, 4)["next_action"])
        n = len(msgs(eng, 4, "outbound"))
        eng.tick()
        eng.handle_inbound(PHONE[4], "where do I go?", "c2")
        self.assertEqual(len(msgs(eng, 4, "outbound")), n)                       # no offer while the clinician owns it
        self.assertIn("New eligible order arrived", escalations(eng, 4)[0]["summary"])
        eng.resolve_escalation(escalations(eng, 4)[0]["id"], "clinician", "proceed", minutes=3)
        self.assertEqual(conv(eng, 4)["state"], "engaged")


class N2_RecipientChange(unittest.TestCase):
    def test_queued_message_is_not_sent_to_old_number_after_partner_phone_change(self):
        eng = make_engine()
        eng.pause("hold")
        eng.handle_inbound(PHONE[1], "where do I go?", "r1")
        self.assertEqual(msgs(eng, 1, "outbound")[-1]["status"], "queued")
        eng.import_orders({"partner_id": "RIVERBEND", "generated_at": eng.now().isoformat(),
                           "orders": [new_order("P-01", "Alice Winslow", "+12075550888", "ORD-1001")]})   # same order id → dup; phone changed
        self.assertEqual(patient(eng, 1)["phone"], "+12075550888")
        eng.resume(); eng.tick()
        offer = [m for m in msgs(eng, 1, "outbound") if m["template_id"] == "offer_sites"][0]
        self.assertEqual(offer["status"], "cancelled")
        self.assertFalse(any(x["to"] == PHONE[1] and "locations" in x["body"] for x in eng.messaging.sent))

    def test_send_time_recipient_check_is_independent_of_import(self):
        eng = make_engine()
        eng.pause("hold")
        eng.handle_inbound(PHONE[2], "where do I go?", "r2")
        eng.conn.execute("UPDATE patients SET phone='+12075550777' WHERE id=?", (patient(eng, 2)["id"],)); eng.conn.commit()
        eng.resume(); eng.tick()
        self.assertEqual([m for m in msgs(eng, 2, "outbound") if m["template_id"] == "offer_sites"][0]["status"], "cancelled")


class N3_InboundRecovery(unittest.TestCase):
    def test_stop_committed_but_unprocessed_is_recovered_on_restart(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "t.sqlite")
        eng = make_engine(db_path=path)
        with mock.patch.object(Engine, "_process_inbound", side_effect=SystemExit("process died")):
            with self.assertRaises(SystemExit):
                eng.handle_inbound(PHONE[5], "STOP", "stop-1")
        self.assertEqual(row(eng.conn, "SELECT status FROM messages WHERE provider_message_id='stop-1'")["status"], "received")
        eng.conn.close()
        conn = connect(path)
        eng2 = Engine(conn, Directory.load(DIRECTORY), MockAdapter(), SimulatedMessaging(), Policy())
        # replay of the same provider id is NOT a duplicate while unprocessed
        r = eng2.handle_inbound(PHONE[5], "STOP", "stop-1")
        self.assertEqual(r["intent"], "opt_out")
        self.assertEqual(row(conn, "SELECT consent_sms FROM patients WHERE source_patient_id='P-05'")["consent_sms"], 0)
        self.assertEqual(row(conn, "SELECT status FROM messages WHERE provider_message_id='stop-1'")["status"], "processed")
        # and a third replay is now a duplicate
        self.assertEqual(eng2.handle_inbound(PHONE[5], "STOP", "stop-1")["reason"], "duplicate")

    def test_tick_recovers_unprocessed_inbound_before_sending(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "t.sqlite")
        eng = make_engine(db_path=path)
        with mock.patch.object(Engine, "_process_inbound", side_effect=SystemExit("died")):
            with self.assertRaises(SystemExit):
                eng.handle_inbound(PHONE[6], "STOP", "stop-2")
        eng.conn.close()
        conn = connect(path)
        eng2 = Engine(conn, Directory.load(DIRECTORY), MockAdapter(), SimulatedMessaging(), Policy())
        eng2.advance(days=3); eng2.import_orders({"partner_id": "RIVERBEND", "generated_at": eng2.now().isoformat(), "orders": []})
        eng2.import_updates(feed([], eng2.now()))
        r = eng2.tick()
        self.assertTrue(any(a.startswith("recovered_inbound") and a.endswith("opt_out") for a in r["actions"]))
        self.assertFalse(any(x["to"] == PHONE[6] and "following up" in x["body"] for x in eng2.messaging.sent))


class N4_EpochWithinBatch(unittest.TestCase):
    def test_state_is_reread_before_each_send_in_the_same_flush(self):
        eng = make_engine(policy=Policy(sms_max_attempts=1))
        eng.pause("hold")
        eng.handle_inbound(PHONE[7], "where do I go?", "e1")
        eng.handle_inbound(PHONE[7], "which one is closest?", "e2")
        self.assertEqual(len([m for m in msgs(eng, 7, "outbound") if m["status"] == "queued"]), 2)
        eng.messaging.fail_times = 1          # the first send in the next flush fails definitively
        eng.resume(); eng.tick()
        st = {m["id"]: m["status"] for m in msgs(eng, 7, "outbound") if m["template_id"] == "offer_sites"}
        self.assertIn("failed", st.values())
        self.assertIn("cancelled", st.values())          # second offer sees the escalation created by the first failure
        self.assertNotIn("sent", st.values())


class N5_BusinessHours(unittest.TestCase):
    def test_deadline_counts_business_hours_and_ack_does_not_stop_overdue(self):
        p = Policy()
        self.assertEqual(add_business_hours(datetime(2026, 9, 18, 16, 30), 4, p), datetime(2026, 9, 21, 11, 30))   # Fri 16:30 → Mon 11:30
        self.assertEqual(add_business_hours(datetime(2026, 9, 15, 10, 0), 4, p), datetime(2026, 9, 15, 14, 0))
        self.assertEqual(add_business_hours(datetime(2026, 9, 15, 21, 0), 4, p), datetime(2026, 9, 16, 12, 0))
        eng = make_engine(policy=v3_policy())
        eng.set_now(datetime(2026, 9, 18, 16, 30))
        eng.handle_inbound(PHONE[8], "do I still need this?", "bh1")
        e = escalations(eng, 8)[0]
        self.assertEqual(e["due_at"], "2026-09-21T11:30:00")
        self.assertEqual(sent(eng, 8)[-1]["template_id"], "clinical_ack_after_hours")   # no "today" promise on a Friday afternoon
        eng.acknowledge_escalation(e["id"], "clinician")
        eng.set_now(datetime(2026, 9, 21, 12, 0)); refresh(eng); r = eng.tick()
        self.assertEqual(escalations(eng, 8)[0]["overdue"], 1)


class N6_OutOfOrderEvents(unittest.TestCase):
    def test_result_before_order_applies_on_replay(self):
        eng = make_engine()
        u = {"source_order_id": "LATER", "kind": "result_finalized", "lines": ["TSH"], "at": eng.now().isoformat()}
        eng.import_updates(feed([u], eng.now()))
        eng.import_orders({"partner_id": "RIVERBEND", "generated_at": eng.now().isoformat(), "orders": [new_order("P-77", "Late Order", "+12075550977", "LATER")]})
        eng.import_updates(feed([u], eng.now()))
        o = row(eng.conn, "SELECT * FROM orders WHERE source_order_id='LATER'")
        self.assertEqual(o["state"], "verified_complete")


class R3_TwilioFailureClasses(unittest.TestCase):
    def _adapter(self):
        os.environ.update({"OCP_TWILIO_SEND_ENABLED": "1", "TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "tok",
                           "TWILIO_FROM_NUMBER": "+15555550000"})
        from ocp.messaging.twilio_adapter import TwilioMessaging
        return TwilioMessaging()

    def tearDown(self):
        for k in ("OCP_TWILIO_SEND_ENABLED", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
            os.environ.pop(k, None)

    def test_timeout_is_ambiguous_4xx_is_rejected_5xx_is_ambiguous(self):
        t = self._adapter()
        with mock.patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            with self.assertRaises(AmbiguousSendError):
                t.send("+12075550101", "hi", "k")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("u", 400, "bad", {}, None)):
            with self.assertRaises(MessagingError):
                t.send("+12075550101", "hi", "k")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("u", 503, "down", {}, None)):
            with self.assertRaises(AmbiguousSendError):
                t.send("+12075550101", "hi", "k")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError(ConnectionRefusedError())):
            with self.assertRaises(MessagingError):
                t.send("+12075550101", "hi", "k")

    def test_engine_marks_ambiguous_and_never_retries(self):
        class Flaky(SimulatedMessaging):
            def send(self, to, body, dedupe_key):
                raise AmbiguousSendError("timeout")
        eng = make_engine(messaging=Flaky(), tick=False)
        eng.tick(); eng.tick()
        m = rows(eng.conn, "SELECT * FROM messages WHERE direction='outbound'")
        self.assertTrue(all(x["status"] == "ambiguous" and x["attempts"] == 1 for x in m))
        self.assertEqual(row(eng.conn, "SELECT COUNT(*) n FROM escalations WHERE reason='sms_ambiguous'")["n"], len(m))


class R8_R13_R14(unittest.TestCase):
    def test_outbound_cap_holds_when_model_cap_is_also_hit(self):
        eng = make_engine(policy=Policy(max_outbound_per_conversation=1, max_model_calls_per_conversation=1))
        eng.conn.execute("UPDATE conversations SET model_calls=1 WHERE id=?", (conv(eng, 9)["id"],)); eng.conn.commit()
        eng.handle_inbound(PHONE[9], "where?", "cap1")
        self.assertEqual(len(sent(eng, 9)), 1)                                  # the initial outreach only

    def test_weekend_plus_time_constraints_intersect(self):
        eng = make_engine(policy=v3_policy())
        eng.handle_inbound(PHONE[10], "only weekends after 1pm", "w1")
        self.assertEqual(msgs(eng, 10, "outbound")[-1]["template_id"], "no_site_matches")
        eng.handle_inbound(PHONE[11], "weekends after 9am works", "w2")
        self.assertIn("Brunswick", msgs(eng, 11, "outbound")[-1]["body"])

    def test_queued_offer_with_expired_directory_is_cancelled_at_send(self):
        eng = make_engine()
        eng.pause("hold")
        eng.handle_inbound(PHONE[12], "where do I go?", "d1")
        eng.advance(days=100); refresh(eng)
        eng.resume(); eng.tick()
        offer = [m for m in msgs(eng, 12, "outbound") if m["template_id"] == "offer_sites"][0]
        self.assertEqual(offer["status"], "cancelled")
        self.assertIn("directory", offer["last_error"])


class R9_AnthropicAdapterFailurePaths(unittest.TestCase):
    """Uses an injected fake client: no SDK import, no network, no credentials."""
    def _resp(self, stop_reason="end_turn", text='{"intent":"willing","confidence":0.9,"barrier":"none","constraints":{}}'):
        class U:  # usage
            input_tokens, output_tokens, cache_read_input_tokens = 900, 80, 0
        class B:
            type = "text"
            def __init__(self, t): self.text = t
        class R:
            pass
        r = R(); r.stop_reason = stop_reason; r.content = [B(text)]; r.usage = U()
        return r

    def _adapter(self, resp):
        from ocp.llm.anthropic_adapter import AnthropicAdapter
        class Msgs:
            def __init__(self, r): self.r = r
            def create(self, **kw): return self.r
        class Client:
            def __init__(self, r): self.messages = Msgs(r)
        return AnthropicAdapter(client=Client(resp))

    def test_success_records_real_usage(self):
        a = self._adapter(self._resp())
        r = a.classify({"history": [], "open_order_tests": [], "agreed_plan": None}, "sure")
        self.assertEqual((r.intent, r.input_tokens, r.output_tokens, r.simulated), ("willing", 900, 80, False))
        self.assertEqual(a.last_attempts[0]["outcome"], "ok")

    def test_refusal_and_bad_json_still_record_consumed_tokens(self):
        from ocp.llm.base import ProviderError
        for resp in (self._resp(stop_reason="refusal"), self._resp(text="not json")):
            a = self._adapter(resp)
            with self.assertRaises(ProviderError):
                a.classify({"history": [], "open_order_tests": [], "agreed_plan": None}, "x")
            att = a.last_attempts[0]
            self.assertEqual((att["outcome"], att["input_tokens"], att["output_tokens"], att["simulated"]), ("error", 900, 80, False))


if __name__ == "__main__":
    unittest.main()
