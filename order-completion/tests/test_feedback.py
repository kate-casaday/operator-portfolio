"""The priority feedback loop: play a patient → flag a response → explain → evidence captured → export a task."""
import json
import os
import tempfile
import unittest

from ocp import feedback as fb
from ocp.db import rows, row, connect
from ocp.directory import Directory
from ocp.engine import Engine
from ocp.llm.mock import MockAdapter
from ocp.messaging.simulated import SimulatedMessaging
from ocp.rules import Policy
from tests.helpers import make_engine, patient, conv, msgs, PHONE, DIRECTORY, SIM_START


class FeedbackLoop(unittest.TestCase):
    def test_flag_capture_export_and_status_lifecycle(self):
        d = tempfile.mkdtemp(); path = os.path.join(d, "t.sqlite")
        eng = make_engine(db_path=path)
        eng.handle_inbound(PHONE[2], "I work until 6", "f1")
        flagged = msgs(eng, 2, "outbound")[-1]
        f = fb.create(eng, flagged["id"], "preference", "It should have offered only the Brunswick lab, and said why.", notes="felt robotic")
        self.assertEqual((f["label"], f["status"]), ("preference", "open"))
        ev = f["evidence"]
        # evidence is automatic and complete
        self.assertEqual(ev["flagged_message"]["id"], flagged["id"])
        self.assertEqual(ev["thread_through_flagged_message"][-1]["id"], flagged["id"])
        self.assertTrue(all(t["id"] <= flagged["id"] for t in ev["thread_through_flagged_message"]))
        self.assertEqual(ev["flagged_message"]["decision"]["rule"], "offer:filter")
        self.assertEqual([p["key"] for p in ev["preferences_active"]], ["after_time", "evening_ok"])
        self.assertEqual(ev["configuration"]["model_adapter"], "mock"); self.assertTrue(ev["configuration"]["model_simulated"])
        self.assertEqual(ev["configuration"]["template_id"], "offer_sites_constrained")
        self.assertIn("{site_1}", ev["configuration"]["template_text"])
        self.assertEqual(ev["configuration"]["overdue_threshold_days"], 45)
        self.assertTrue(ev["software"]["ocp_version"]); self.assertTrue(ev["software"]["git_commit"])
        self.assertTrue(ev["patient"]["synthetic"])
        self.assertEqual(ev["conversation"]["state"], "engaged")
        self.assertTrue(ev["orders"])
        # Kate's interpretation is stored separately from the evidence
        self.assertNotIn("should_have", ev)
        # durable across restart
        eng.conn.close()
        conn = connect(path)
        eng2 = Engine(conn, Directory.load(DIRECTORY), MockAdapter(), SimulatedMessaging(), Policy())
        self.assertEqual(fb.list_all(eng2)[0]["id"], f["id"])
        self.assertEqual(fb.get(eng2, f["id"])["should_have"], f["should_have"])
        # export
        out = fb.export_task(eng2, f["id"], out_dir=os.path.join(d, "exports"))
        md = out["markdown"]
        self.assertTrue(os.path.exists(out["path"]))
        self.assertIn("SYNTHETIC DATA ONLY", md); self.assertTrue(os.path.exists(out["evidence_path"]))
        self.assertIn("Kate's interpretation", md); self.assertIn("Evidence (captured automatically", md)
        self.assertIn("I work until 6", md); self.assertIn("<-- FLAGGED", md)
        self.assertIn("offer:filter", md)
        self.assertIn("Proposed acceptance criteria", md); self.assertIn("Regression test stub", md)
        self.assertIn("handle_inbound(PHONE[2], 'I work until 6'", md)
        # status lifecycle links a later change to its origin
        fb.update_status(eng2, f["id"], "linked", linked_ref="commit abc123")
        fb.update_status(eng2, f["id"], "verified", verification_note="tests/test_feedback_1.py passes")
        g = fb.get(eng2, f["id"])
        self.assertEqual((g["status"], g["linked_ref"], g["verification_note"]), ("verified", "commit abc123", "tests/test_feedback_1.py passes"))
        kinds = [e["kind"] for e in rows(conn, "SELECT kind FROM events WHERE conversation_id=?", (conv(eng2, 2)["id"],))]
        self.assertIn("feedback_created", kinds); self.assertIn("feedback_exported", kinds); self.assertIn("feedback_status", kinds)

    def test_validation_and_synthetic_guard(self):
        eng = make_engine()
        m = msgs(eng, 3, "outbound")[-1]
        with self.assertRaises(ValueError):
            fb.create(eng, m["id"], "bug", "x")
        with self.assertRaises(ValueError):
            fb.create(eng, m["id"], "defect", "   ")
        f = fb.create(eng, m["id"], "question", "Why does the first text name the provider?")
        eng.conn.execute("UPDATE patients SET synthetic=0 WHERE id=?", (patient(eng, 3)["id"],)); eng.conn.commit()
        f2 = fb.create(eng, m["id"], "defect", "should not matter")
        with self.assertRaises(PermissionError):
            fb.export_task(eng, f2["id"], out_dir=tempfile.mkdtemp())
        # a question exports with question-shaped criteria
        eng.conn.execute("UPDATE patients SET synthetic=1 WHERE id=?", (patient(eng, 3)["id"],)); eng.conn.commit()
        out = fb.export_task(eng, f["id"], out_dir=tempfile.mkdtemp())
        self.assertIn("QUESTION, not a change request", out["markdown"])

    def test_feedback_on_a_scheduled_message_has_no_inbound(self):
        eng = make_engine()
        m = msgs(eng, 4, "outbound")[0]          # initial outreach, no inbound before it
        f = fb.create(eng, m["id"], "defect", "Should not name the ordering provider before identity is confirmed.")
        out = fb.export_task(eng, f["id"], out_dir=tempfile.mkdtemp())
        self.assertIn("no inbound messages before the flagged one", out["markdown"])
        self.assertIn("scheduled", out["markdown"])


if __name__ == "__main__":
    unittest.main()
