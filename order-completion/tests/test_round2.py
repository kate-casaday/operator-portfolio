"""Round-two regression tests for Codex's stage-1 findings Q1–Q5 (all reproduced from ordinary entry points)."""
import os
import socket
import tempfile
import unittest
import urllib.error
from unittest import mock

from ocp.db import rows, row, connect
from ocp.directory import Directory
from ocp.engine import Engine
from ocp.llm.mock import MockAdapter
from ocp.messaging.simulated import SimulatedMessaging
from ocp.messaging.base import MessagingError, AmbiguousSendError
from ocp.rules import Policy
from tests.helpers import make_engine, patient, conv, orders, msgs, escalations, feed, refresh, PHONE, SIM_START, DIRECTORY, v3_policy


def sent(eng, n):
    return [m for m in msgs(eng, n, "outbound") if m["status"] == "sent"]


def sent_to(eng, phone):
    return [x for x in eng.messaging.sent if x["to"] == phone]


def reopen(path):
    conn = connect(path)
    return Engine(conn, Directory.load(DIRECTORY), MockAdapter(), SimulatedMessaging(), Policy())


def order_feed(pid_src, name, phone, oid):
    return {"partner_id": "RIVERBEND", "generated_at": SIM_START.isoformat(), "orders": [
        {"source_order_id": oid, "patient": {"source_patient_id": pid_src, "display_name": name, "phone": phone, "consent_sms": True,
                                             "home_town": "Bath"}, "ordered_at": "2026-07-01T09:00:00", "ordering_provider": "Dr. S",
         "priority": "routine", "lines": [{"test_code": "TSH", "test_name": "TSH"}]}]}


class Q1_PendingStopIsASendBarrier(unittest.TestCase):
    def test_two_pending_inbounds_where_second_is_stop_send_nothing_but_the_confirmation(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "t.sqlite")
        eng = make_engine(db_path=path)
        with mock.patch.object(Engine, "_process_inbound", side_effect=SystemExit("died")):
            for body, pid in (("where do I go?", "q1-a"), ("STOP", "q1-b")):
                with self.assertRaises(SystemExit):
                    eng.handle_inbound(PHONE[1], body, pid)
        eng.conn.close()
        eng2 = reopen(path)
        eng2.tick()
        new_templates = [x["body"][:30] for x in eng2.messaging.sent if x["to"] == PHONE[1]]
        self.assertEqual(len(sent_to(eng2, PHONE[1])), 1)
        self.assertIn("unsubscribed", sent_to(eng2, PHONE[1])[0]["body"])          # only the STOP confirmation
        self.assertEqual(conv(eng2, 1)["state"], "closed")
        self.assertEqual(row(eng2.conn, "SELECT consent_sms FROM patients WHERE source_patient_id='P-01'")["consent_sms"], 0)

    def test_another_patients_inbound_cannot_flush_a_queued_offer_past_a_pending_stop(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "t.sqlite")
        eng = make_engine(db_path=path)
        eng.pause("hold")
        eng.handle_inbound(PHONE[1], "where do I go?", "q1-c")
        eng.resume()
        with mock.patch.object(Engine, "_process_inbound", side_effect=SystemExit("died")):
            with self.assertRaises(SystemExit):
                eng.handle_inbound(PHONE[1], "STOP", "q1-d")
        eng.conn.close()
        eng2 = reopen(path)
        eng2.handle_inbound(PHONE[2], "HELP", "q1-e")                            # some other patient's inbound triggers a flush
        p1 = sent_to(eng2, PHONE[1])
        self.assertEqual(len(p1), 1)
        self.assertIn("unsubscribed", p1[0]["body"])
        offer = [m for m in msgs(eng2, 1, "outbound") if m["template_id"] == "offer_sites"][0]
        self.assertIn(offer["status"], ("cancelled", "suppressed"))
        self.assertEqual(row(eng2.conn, "SELECT status FROM messages WHERE provider_message_id='q1-d'")["status"], "processed")

    def test_errored_stop_still_blocks_sends_to_that_number(self):
        eng = make_engine()
        eng.pause("hold")
        eng.handle_inbound(PHONE[3], "where do I go?", "q1-f")
        eng.resume()
        # simulate a STOP whose processing failed with a software error and is sitting in status 'error'
        eng.conn.execute("INSERT INTO messages(conversation_id,direction,kind,epoch,from_phone,body,segments,provider,provider_message_id,status,created_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (conv(eng, 3)["id"], "inbound", "reply", 0, PHONE[3], "STOP", 1, "simulated", "q1-g", "error", "2026-09-15T10:00:00"))
        eng.conn.commit()
        r = eng.tick()
        self.assertTrue(any(a.startswith("held_pending_stop") for a in r["actions"]))
        self.assertFalse(any("locations" in x["body"] for x in sent_to(eng, PHONE[3])))


class Q2_RecoveryBindsToOriginalSender(unittest.TestCase):
    def test_wrong_number_recovered_after_partner_phone_change_suppresses_old_number_only(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "t.sqlite")
        eng = make_engine(db_path=path)
        with mock.patch.object(Engine, "_process_inbound", side_effect=SystemExit("died")):
            with self.assertRaises(SystemExit):
                eng.handle_inbound(PHONE[1], "wrong number", "q2-a")
        eng.conn.close()
        eng2 = reopen(path)
        eng2.import_orders(order_feed("P-01", "Alice Winslow", "+12075550888", "ORD-1001"))   # partner changed the number
        eng2.tick()
        self.assertEqual(row(eng2.conn, "SELECT phone FROM patients WHERE source_patient_id='P-01'")["phone"], "+12075550888")  # NOT cleared
        self.assertTrue(row(eng2.conn, "SELECT 1 FROM suppressed_numbers WHERE phone=?", (PHONE[1],)))
        self.assertIsNone(row(eng2.conn, "SELECT 1 FROM suppressed_numbers WHERE phone='+12075550888'"))
        self.assertEqual([x["to"] for x in eng2.messaging.sent if "mix-up" in x["body"]], [PHONE[1]])   # confirmation to the SENDER
        self.assertTrue(any(e["reason"] == "identity_uncertain" for e in escalations(eng2, 1)))

    def test_stop_recovered_after_phone_change_does_not_opt_out_the_new_number(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "t.sqlite")
        eng = make_engine(db_path=path)
        with mock.patch.object(Engine, "_process_inbound", side_effect=SystemExit("died")):
            with self.assertRaises(SystemExit):
                eng.handle_inbound(PHONE[2], "STOP", "q2-b")
        eng.conn.close()
        eng2 = reopen(path)
        eng2.import_orders(order_feed("P-02", "Ben Ortiz", "+12075550889", "ORD-1002"))
        eng2.tick()
        p = row(eng2.conn, "SELECT * FROM patients WHERE source_patient_id='P-02'")
        self.assertEqual((p["phone"], p["local_opt_out"]), ("+12075550889", 0))
        self.assertTrue(row(eng2.conn, "SELECT 1 FROM suppressed_numbers WHERE phone=?", (PHONE[2],)))
        self.assertNotEqual(conv(eng2, 2)["state"], "closed")


class Q3_TwilioWrappedErrors(unittest.TestCase):
    def setUp(self):
        os.environ.update({"OCP_TWILIO_SEND_ENABLED": "1", "TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "tok", "TWILIO_FROM_NUMBER": "+15555550000"})

    def tearDown(self):
        for k in ("OCP_TWILIO_SEND_ENABLED", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
            os.environ.pop(k, None)

    def test_reset_and_broken_pipe_are_ambiguous_refused_and_dns_are_retryable(self):
        from ocp.messaging.twilio_adapter import TwilioMessaging
        t = TwilioMessaging()
        for reason, exc in ((ConnectionResetError("reset"), AmbiguousSendError), (BrokenPipeError("pipe"), AmbiguousSendError),
                            (ConnectionRefusedError(), MessagingError), (socket.gaierror("dns"), MessagingError),
                            (OSError("other"), AmbiguousSendError)):
            with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError(reason)):
                with self.assertRaises(exc, msg=repr(reason)):
                    t.send("+12075550101", "hi", "k")


class Q4_OfferSelectionIsEpisodeScoped(unittest.TestCase):
    def test_relative_choice_after_new_order_does_not_use_previous_episodes_offer(self):
        eng = make_engine()
        eng.handle_inbound(PHONE[1], "where do I go?", "q4-a")
        eng.import_updates(feed([{"source_order_id": "ORD-1001", "kind": "result_finalized", "lines": ["CBC"], "at": eng.now().isoformat()}], eng.now()))
        eng.import_orders(order_feed("P-01", "Alice Winslow", PHONE[1], "ORD-1001B"))
        eng.tick()
        eng.handle_inbound(PHONE[1], "the first one Friday", "q4-b")
        self.assertNotEqual(conv(eng, 1)["state"], "plan_agreed")
        self.assertEqual(msgs(eng, 1, "outbound")[-1]["template_id"], "offer_sites")      # fresh offer for this episode
        eng.handle_inbound(PHONE[1], "the first one Friday", "q4-c")
        self.assertEqual(conv(eng, 1)["state"], "plan_agreed")

    def test_unsent_offer_cannot_be_selected(self):
        eng = make_engine()
        eng.pause("hold")
        eng.handle_inbound(PHONE[2], "where do I go?", "q4-d")     # offer queued, not sent
        eng.handle_inbound(PHONE[2], "the second one Friday", "q4-e")
        self.assertNotEqual(conv(eng, 2)["state"], "plan_agreed")


class Q5_ContentApprovalRevalidated(unittest.TestCase):
    def _expire_instructions(self, eng):
        for k in eng.directory._instructions:
            eng.directory._instructions[k]["approved_at"] = "2026-01-01T00:00:00"

    def test_queued_offer_with_expired_link_approval_is_cancelled_even_when_sites_are_valid(self):
        eng = make_engine()
        eng.pause("hold")
        eng.handle_inbound(PHONE[3], "where do I go?", "q5-a")
        self._expire_instructions(eng)
        eng.resume(); eng.tick()
        offer = [m for m in msgs(eng, 3, "outbound") if m["template_id"] == "offer_sites"][0]
        self.assertEqual(offer["status"], "cancelled")
        self.assertFalse(any("example.invalid" in x["body"] for x in sent_to(eng, PHONE[3])))
        self.assertEqual(conv(eng, 3)["next_action"], "followup")

    def test_queued_transport_ack_with_expired_instruction_is_cancelled(self):
        eng = make_engine(policy=v3_policy())
        eng.pause("hold")
        eng.handle_inbound(PHONE[4], "no car, can't get there", "q5-b")
        self._expire_instructions(eng)
        eng.resume(); eng.tick()
        m = [m for m in msgs(eng, 4, "outbound") if m["template_id"] == "transport_ack"][0]
        self.assertEqual(m["status"], "cancelled")
        self.assertFalse(any("free ride" in x["body"] for x in sent_to(eng, PHONE[4])))

    def test_site_reverification_changes_cancel_stale_offer_body(self):
        eng = make_engine()
        eng.pause("hold")
        eng.handle_inbound(PHONE[5], "where do I go?", "q5-c")
        for s in eng.directory._sites:
            if s["id"] == "RB-BATH":
                s["verified_at"] = eng.now().isoformat()          # re-verified: content revision changed
        eng.resume(); eng.tick()
        offer = [m for m in msgs(eng, 5, "outbound") if m["template_id"] == "offer_sites"][0]
        self.assertEqual(offer["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
