"""The scripted demo must pass in the test suite too, so the walkthrough and the tests cannot drift."""
import unittest

from ocp.scenarios import run_demo
from tests.helpers import make_engine


class DemoReplay(unittest.TestCase):
    def test_all_scripted_scenarios_pass(self):
        eng = make_engine(import_feed=False)
        results = run_demo(eng, quiet=True)
        failed = [(r["id"], [c for c in r["checks"] if not c[1]]) for r in results if not r["passed"]]
        self.assertEqual(failed, [])
        self.assertEqual(len(results), 41)

    def test_zero_false_completions_and_zero_messages_after_suppression(self):
        from ocp.db import rows
        eng = make_engine(import_feed=False)
        run_demo(eng, quiet=True)
        # every verified order has a partner result_finalized event
        for o in rows(eng.conn, "SELECT * FROM orders WHERE state='verified_complete'"):
            ev = rows(eng.conn, "SELECT * FROM events WHERE order_id=? AND kind='result_finalized'", (o["id"],))
            self.assertTrue(ev, o["source_order_id"])
        # nothing SENT (by sent_at, not queue time) at or after a suppression on that patient, except the confirmation
        for s in rows(eng.conn, "SELECT e.*, c.id cid FROM events e JOIN conversations c ON c.patient_id=e.patient_id WHERE e.kind='suppressed'"):
            later = rows(eng.conn, "SELECT * FROM messages WHERE conversation_id=? AND direction='outbound' "
                                   "AND status='sent' AND sent_at>=? AND template_id NOT IN "
                                   "('opt_out_confirm','wrong_number_confirm')", (s["cid"], s["at"]))
            self.assertEqual(later, [])
        # no message sent to a suppressed number after suppression
        for n in rows(eng.conn, "SELECT * FROM suppressed_numbers"):
            later = rows(eng.conn, "SELECT * FROM messages WHERE to_phone=? AND status='sent' AND sent_at>? AND template_id NOT IN "
                                   "('opt_out_confirm','wrong_number_confirm')", (n["phone"], n["at"]))
            self.assertEqual(later, [])


if __name__ == "__main__":
    unittest.main()
