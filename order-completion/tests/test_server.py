"""Dashboard/API smoke test: real HTTP server in a thread on an ephemeral port, real requests."""
import json
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from ocp.server import make_handler
from tests.helpers import make_engine, PHONE, conv


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = make_engine()
        try:
            cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.eng))
        except OSError as e:   # a sandbox that cannot bind a loopback port: skip with a message rather than error
            raise unittest.SkipTest("cannot bind 127.0.0.1 in this environment (%s); HTTP tests skipped" % e)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=5) as r:
            return r.status, r.read().decode()

    def _post(self, path, data=None, form=False, headers=None):
        if form:
            body = urllib.parse.urlencode(data or {}).encode()
            ctype = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data or {}).encode()
            ctype = "application/json"
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=body, method="POST")
        req.add_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode() or "{}") if r.headers.get("Content-Type", "").startswith("application/json") else r.read().decode()

    def test_dashboard_and_state(self):
        status, html = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("SIMULATED", html)
        status, body = self._get("/api/state")
        s = json.loads(body)
        self.assertTrue(s["adapters"]["model_simulated"])
        self.assertTrue(s["adapters"]["messaging_simulated"])
        self.assertEqual(s["summary"]["patients"], 29)
        self.assertEqual(len(s["conversations"]), 25)

    def test_pause_resume_inbound_escalation_resolve_flow(self):
        _, r = self._post("/api/pause", {"reason": "smoke"})
        self.assertEqual(r["paused"], "kate:smoke")
        _, r = self._post("/api/resume")
        self.assertIsNone(r["paused"])
        _, r = self._post("/api/sim/inbound", {"phone": PHONE[3], "body": "how much does it cost?"})
        self.assertEqual(r["intent"], "cost_question")
        eid = r["escalation"]
        _, s = self._get("/api/state")
        s = json.loads(s)
        self.assertTrue(any(e["id"] == eid for e in s["kate_queue"]))
        _, r = self._post("/api/escalations/%d/acknowledge" % eid)
        self.assertTrue(r["ok"])
        _, r = self._post("/api/escalations/%d/resolve" % eid, {"resolution": "sent verified estimate", "minutes": 4})
        self.assertTrue(r["ok"]); self.assertEqual(r["minutes"], 4.0)
        _, t = self._get("/api/conversation/%d" % conv(self.eng, 3)["id"])
        t = json.loads(t)
        self.assertEqual(t["escalations"][0]["status"], "resolved")
        _, r = self._post("/api/link_click", {"conversation_id": conv(self.eng, 3)["id"]})
        self.assertFalse(r["state_changed"])
        _, r = self._post("/api/tick")
        self.assertIn("actions", r)
        _, r = self._post("/api/advance", {"hours": 2})
        self.assertTrue(r["ok"])
        _, r = self._post("/api/human_time", {"activity": "dashboard review", "minutes": 10})
        self.assertTrue(r["ok"])

    def test_feedback_threshold_costs_decisions_endpoints(self):
        _, r = self._post("/api/sim/inbound", {"phone": PHONE[7], "body": "I work until 6"})
        _, t = self._get("/api/conversation/%d" % r["conversation_id"]); t = json.loads(t)
        flagged = [m for m in t["messages"] if m["direction"] == "outbound"][-1]
        self.assertIn("rule", flagged["decision"])
        _, f = self._post("/api/feedback", {"message_id": flagged["id"], "label": "defect", "should_have": "offer Brunswick only"})
        self.assertEqual(f["status"], "open")
        _, e = self._post("/api/feedback/%d/export" % f["id"])
        self.assertTrue(e["ok"]); self.assertIn("feedback/exports/", e["path"])
        _, st = self._post("/api/feedback/%d/status" % f["id"], {"status": "linked", "linked_ref": "abc"})
        self.assertEqual(st["status"], "linked")
        _, lst = self._get("/api/feedback"); self.assertEqual(json.loads(lst)["feedback"][0]["id"], f["id"])
        _, th = self._post("/api/threshold", {"days": 20}); self.assertEqual(th["overdue_threshold_days"], 20)
        status, body = self._get("/api/costs"); self.assertIn("measured", json.loads(body))
        status, body = self._get("/api/decisions"); self.assertTrue(json.loads(body)["decisions"])
        _, pr = self._post("/api/preference", {"patient_id": t["conversation"]["patient_id"], "key": "town", "value": "Bath"})
        self.assertTrue(pr["recorded"])
        _, s = self._get("/api/state"); s = json.loads(s)
        self.assertIn("kate_queue", s); self.assertIn("clinician_queue", s); self.assertEqual(s["config"]["overdue_threshold_days"], 20)

    def test_twilio_webhook_shape_with_simulated_adapter(self):
        # With the simulated adapter active there is no signature check; the body is parsed like Twilio's form post.
        status, body = self._post("/webhook/twilio", {"From": PHONE[5], "Body": "STOP", "MessageSid": "SMwebhook1"}, form=True)
        self.assertEqual(status, 200)
        self.assertIn("<Response>", body)
        self.assertEqual(conv(self.eng, 5)["state"], "closed")


if __name__ == "__main__":
    unittest.main()
