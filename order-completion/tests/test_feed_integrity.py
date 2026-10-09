"""Feed integrity (Sept 23, 2026; reconciled after Codex's first review the same day): the arrival path validates every
partner file (both streams), quarantines bad rows and holds the patients they touch, holds bad files with nothing applied
(one transaction), enforces the block at the send boundary, notices what did not arrive and re-arms after recovery,
retains the payload, and tells the partner's technical contact something specific without disclosing values.
Operating principle under test: never outsource to the health system's technology team work we can do ourselves.
Everything synthetic; nothing leaves the process."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

from ocp import feed_integrity as FI, scheduling as S
from ocp.db import rows, row, connect
from ocp.rules import Policy
from ocp.server import make_handler, ops_payload
from tests.helpers import make_engine, patient, conv, orders, msgs, feed, refresh, PHONE
from ocp.scenarios import SIM_START


def order(pid, oid, phone, name="Some Body", **kw):
    rec = {"source_order_id": oid, "patient": {"source_patient_id": pid, "display_name": name, "phone": phone, "consent_sms": True, "home_town": "Bath"},
           "ordered_at": "2026-07-01T09:00:00", "ordering_provider": "Dr. S", "priority": "routine", "state": "open",
           "lines": [{"test_code": "TSH", "test_name": "TSH"}]}
    rec.update(kw)
    return rec


def ofeed(eng, orders_, at=None, **extra):
    f = {"partner_id": "RIVERBEND", "generated_at": (at or eng.now()).isoformat(), "orders": orders_}
    f.update(extra)
    return f


def later(eng, hours=1):
    eng.advance(hours=hours)
    return eng.now()


def alerts(eng, code=None):
    q = "SELECT * FROM feed_alerts" + (" WHERE code=?" if code else "") + " ORDER BY id"
    return rows(eng.conn, q, (code,) if code else ())


def failed(r):
    return [c["check"] for c in r["checks"] if c["status"] == "fail"]


def warned(r):
    return [c["check"] for c in r["checks"] if c["status"] == "warn"]


def snapshot(eng):
    """Everything a rejected file must leave untouched."""
    out = {}
    for t in ("patients", "orders", "order_lines", "order_events", "conversations", "messages", "feed_imports", "feed_events", "bookings", "escalations"):
        out[t] = [tuple(r) for r in eng.conn.execute("SELECT * FROM %s ORDER BY 1" % t).fetchall()]
    return out


class FI_Atomic(unittest.TestCase):
    def test_schema_creation_never_commits_a_transaction_in_progress(self):
        conn = connect(":memory:")
        for m in (S, FI):
            m.ensure_schema(conn)
        conn.commit()
        conn.execute("INSERT INTO settings(key,value) VALUES('probe','1')")
        self.assertTrue(conn.in_transaction)
        S.ensure_schema(conn); FI.ensure_schema(conn)                 # would COMMIT if it ran executescript
        self.assertTrue(conn.in_transaction)
        conn.rollback()
        self.assertIsNone(row(conn, "SELECT 1 FROM settings WHERE key='probe'"))

    def test_a_failure_late_in_a_multi_event_file_applies_nothing_and_the_receipt_says_so(self):
        eng = make_engine()
        later(eng)
        calls = {"n": 0}
        real = eng._after_order_terminal

        def boom(pid):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated crash on the second event")
            return real(pid)
        eng._after_order_terminal = boom
        before = snapshot(eng)
        r = eng.receive_feed(feed([{"source_order_id": "ORD-1001", "kind": "attended", "at": eng.now().isoformat()},
                                   {"source_order_id": "ORD-1002", "kind": "cancelled", "at": eng.now().isoformat()},
                                   {"source_order_id": "ORD-1003", "kind": "cancelled", "at": eng.now().isoformat()}], eng.now()), source_name="multi.json")
        self.assertEqual(r["verdict"], "rejected"); self.assertFalse(r["applied"]); self.assertIn("import", failed(r))
        self.assertEqual(snapshot(eng), before)                                                     # nothing from the file survived
        rc = row(eng.conn, "SELECT * FROM feed_receipts WHERE id=?", (r["receipt_id"],)); self.assertEqual(rc["applied"], 0)
        self.assertIn("rolled back", [c["detail"] for c in r["checks"] if c["check"] == "import"][0])

    def test_offset_timestamps_are_accepted_canonicalised_and_never_crash_the_importer(self):
        eng = make_engine(); later(eng)
        r = eng.receive_feed(feed([{"source_order_id": "ORD-1002", "kind": "replaced", "at": eng.now().isoformat() + "Z",
                                    "replacement": {"source_order_id": "ORD-1002B", "ordered_at": "2026-07-02T09:00:00Z", "intended_due_at": "2026-10-01T09:00:00Z", "lines": [{"test_code": "TSH", "test_name": "TSH"}]}}],
                                  eng.now()))
        self.assertNotEqual(r["verdict"], "rejected"); self.assertIn("timezone", warned(r))
        o = row(eng.conn, "SELECT * FROM orders WHERE source_order_id='ORD-1002B'")
        self.assertEqual(o["intended_due_at"], "2026-10-01T09:00:00")                                 # stored on the naive contract
        f = ofeed(eng, [order("P-901", "ORD-901", "+12075550901", ordered_at="2026-07-01T09:00:00-04:00")], at=None); f["generated_at"] = eng.now().isoformat() + "+00:00"
        r = eng.receive_feed(f); self.assertNotEqual(r["verdict"], "rejected")


class FI_MixedStreams(unittest.TestCase):
    def test_a_payload_with_orders_and_events_validates_both_and_applies_both(self):
        eng = make_engine(); later(eng)
        f = ofeed(eng, [order("P-902", "ORD-902", "+12075550902")], updates=[{"source_order_id": "ORD-1001", "kind": "cancelled", "at": eng.now().isoformat()}])
        r = eng.receive_feed(f)
        self.assertEqual((r["verdict"], r["kind"]), ("accepted", "orders+updates"))
        self.assertIsNotNone(row(eng.conn, "SELECT 1 FROM patients WHERE source_patient_id='P-902'"))
        self.assertEqual(row(eng.conn, "SELECT state FROM orders WHERE source_order_id='ORD-1001'")["state"], "cancelled_by_partner")
        self.assertTrue({c["check"] for c in r["checks"]} >= {"rows:orders", "rows:updates", "sequence:orders", "sequence:updates"})

    def test_a_malformed_event_holds_the_whole_payload_including_its_valid_orders(self):
        eng = make_engine(); later(eng)
        before = snapshot(eng)
        f = ofeed(eng, [order("P-903", "ORD-903", "+12075550903")], order_events=[{"source_order_id": "ORD-1001", "kind": "result_finalized", "at": eng.now().isoformat()}])   # no lines
        r = eng.receive_feed(f)
        self.assertEqual(r["verdict"], "rejected"); self.assertIn("rows:updates", failed(r))
        self.assertEqual(snapshot(eng), before)
        f = ofeed(eng, [], updates=[{"source_order_id": "ORD-1001", "kind": "cancelled", "at": eng.now().isoformat()}])   # empty orders + one good event
        r = eng.receive_feed(f); self.assertNotEqual(r["verdict"], "rejected"); self.assertEqual(row(eng.conn, "SELECT state FROM orders WHERE source_order_id='ORD-1001'")["state"], "cancelled_by_partner")


class FI_Arrival(unittest.TestCase):
    def test_unreadable_and_mistyped_inputs_are_rejected_with_a_receipt_never_raised(self):
        eng = make_engine()
        for payload, name in ((b"\xff\xfe not utf8", "bytes"), (b"{not json", "json"), (b"[1,2]", "array"), (b"3", "scalar"), (7, "int"),
                              ({"partner_id": {}, "generated_at": eng.now().isoformat(), "orders": []}, "partner-dict"),
                              ({"partner_id": "RIVERBEND", "generated_at": eng.now().isoformat(), "orders": 3}, "orders-int"),
                              ({"partner_id": "RIVERBEND", "generated_at": eng.now().isoformat(), "updates": [{"source_order_id": "ORD-1001", "kind": [], "at": eng.now().isoformat()}]}, "kind-list"),
                              ({"partner_id": "RIVERBEND", "generated_at": eng.now().isoformat(), "patients": [1], "orders": []}, "patients-scalar")):
            r = eng.receive_feed(payload, source="push", source_name=name)
            self.assertEqual(r["verdict"], "rejected", name); self.assertFalse(r["applied"])
            self.assertIsNotNone(row(eng.conn, "SELECT 1 FROM feed_receipts WHERE id=? AND verdict='rejected'", (r["receipt_id"],)), name)
        self.assertEqual(alerts(eng, "internal_error"), [])                                              # each was a diagnosed rejection, not an exception
        rc = row(eng.conn, "SELECT * FROM feed_receipts WHERE source_name='bytes'"); self.assertEqual(rc["byte_size"], 11); self.assertTrue(rc["sha1"])

    def test_envelope_partner_shape_and_sequence_checks(self):
        eng = make_engine(); later(eng)
        f = ofeed(eng, [order("P-990", "ORD-990", "+12075550190")]); del f["generated_at"]
        self.assertEqual(eng.receive_feed(f)["verdict"], "rejected")
        r = eng.receive_feed(ofeed(eng, [order("P-990", "ORD-990", "+12075550190")], at=eng.now() + timedelta(days=2)))
        self.assertEqual(r["verdict"], "rejected"); self.assertIn("future", [c["detail"] for c in r["checks"] if c["check"] == "envelope"][0])
        r = eng.receive_feed(dict(ofeed(eng, [order("P-990", "ORD-990", "+12075550190")]), partner_id="OTHERSYSTEM"), source_name="wrong.json")
        self.assertEqual(r["verdict"], "rejected")
        a = alerts(eng, "file_rejected")[-1]
        self.assertEqual(a["partner_id"], "RIVERBEND"); self.assertIn("another partner", a["message_to_partner"]); self.assertIn("wrong.json", a["message_to_partner"])
        self.assertEqual(len(alerts(eng, "file_rejected")), 3)                                           # three different failures, three messages
        self.assertEqual(eng.receive_feed({"partner_id": "RIVERBEND", "generated_at": eng.now().isoformat(), "somethingelse": []})["verdict"], "rejected")
        self.assertIsNone(row(eng.conn, "SELECT 1 FROM patients WHERE source_patient_id='P-990'"))
        # the wrong-partner and envelope failures did not block this partner (unknown stream); a held stream does
        self.assertIsNone(FI.blocked(eng))

    def test_replay_conflict_late_arrival_and_stalled_export_are_four_different_things(self):
        eng = make_engine(policy=Policy(feed_baseline_min_files=2)); later(eng)
        good = ofeed(eng, [order("P-904", "ORD-904", "+12075550904")]); self.assertEqual(eng.receive_feed(good, source_name="a.json")["verdict"], "accepted")
        h0 = FI._health(eng.conn, "RIVERBEND", "orders")
        # same time, different content → conflict, held
        r = eng.receive_feed(ofeed(eng, [order("P-905", "ORD-905", "+12075550905")]), source_name="b.json"); self.assertEqual(r["verdict"], "rejected"); self.assertIn("sequence:orders", failed(r))
        self.assertTrue(FI.blocked(eng))
        # replay of the accepted file: a NO-OP on state — nothing imported, block kept, baseline untouched
        snap = snapshot(eng)
        r = eng.receive_feed(good, source_name="a-again.json"); self.assertEqual(r["verdict"], "accepted_replay"); self.assertFalse(r["applied"])
        self.assertEqual(snapshot(eng), snap)
        self.assertTrue(FI.blocked(eng)); h1 = FI._health(eng.conn, "RIVERBEND", "orders")
        self.assertEqual((h1["baseline_n"], h1["accepted_files"]), (h0["baseline_n"], h0["accepted_files"]))
        # a newer good file clears it
        later(eng); r = eng.receive_feed(ofeed(eng, [order("P-906", "ORD-906", "+12075550906")]), source_name="c.json"); self.assertNotEqual(r["verdict"], "rejected"); self.assertIsNone(FI.blocked(eng))
        # an older file arriving after a current good one: held, partner told, but NO block (the current file stays in use)
        r = eng.receive_feed(ofeed(eng, [order("P-907", "ORD-907", "+12075550907")], at=eng.now() - timedelta(hours=5)), source_name="old.json")
        self.assertEqual(r["verdict"], "rejected"); self.assertIsNone(FI.blocked(eng))
        a = alerts(eng, "file_rejected")[-1]; self.assertIn("most recent good file remains in use", a["message_to_partner"]); self.assertIn("old.json", a["message_to_partner"])
        self.assertIsNone(row(eng.conn, "SELECT 1 FROM patients WHERE source_patient_id='P-907'"))
        # the same older file behind a STALE good file is a stalled export: held AND blocked
        eng.advance(hours=60)
        r = eng.receive_feed(ofeed(eng, [order("P-908", "ORD-908", "+12075550908")], at=eng.now() - timedelta(hours=70)), source_name="older.json")
        self.assertEqual(r["verdict"], "rejected"); self.assertTrue(FI.blocked(eng))

    def test_bad_rows_are_quarantined_the_rest_import_and_the_partner_hears_ids_and_reasons_only(self):
        eng = make_engine(); later(eng)
        bad = order("P-892", "ORD-892", "207-555-0192")            # not E.164
        good = order("P-893", "ORD-893", "+12075550193")
        r = eng.receive_feed(ofeed(eng, [bad, good]), source_name="riverbend-orders-0924.json")
        self.assertEqual(r["verdict"], "accepted_with_warnings"); self.assertEqual(r["quarantined"], 1)
        self.assertIsNotNone(row(eng.conn, "SELECT 1 FROM patients WHERE source_patient_id='P-893'"))
        self.assertIsNone(row(eng.conn, "SELECT 1 FROM patients WHERE source_patient_id='P-892'"))
        a = alerts(eng, "rows_quarantined")[-1]
        self.assertTrue(a["partner_action_needed"]); self.assertEqual(a["notify_channel"], "simulated"); self.assertTrue(a["notified_at"])
        for needle in ("riverbend-orders-0924.json", "ORD-892", "E.164", "What we did", "What we need", "receipt %d" % r["receipt_id"]):
            self.assertIn(needle, a["message_to_partner"])
        self.assertNotIn("207-555-0192", a["message_to_partner"]); self.assertNotIn("Some Body", a["message_to_partner"])   # no values, ever
        self.assertNotIn("next export will pick them up", a["message_to_partner"])
        self.assertEqual(eng.tech_contact.sent[-1]["to"], "integration@example.invalid")
        rc = row(eng.conn, "SELECT * FROM feed_receipts WHERE id=?", (r["receipt_id"],))
        self.assertEqual(json.loads(rc["quarantined"])[0]["source_id"], "ORD-892")

    def test_quarantine_holds_an_existing_patient_until_a_clean_row_arrives(self):
        eng = make_engine(); later(eng)
        p1 = patient(eng, 1)
        r = eng.receive_feed(ofeed(eng, [order("P-01", "ORD-1001X", "bad-phone", name=p1["display_name"]), order("P-908", "ORD-908", "+12075550908"), order("P-909", "ORD-909", "+12075550909"), order("P-910", "ORD-910", "+12075550910")]))
        self.assertEqual(r["verdict"], "accepted_with_warnings")
        self.assertEqual(FI.patient_held(eng.conn, p1), "patient.phone is not E.164 (+ and 8-15 digits)")
        self.assertEqual(patient(eng, 1)["phone"], PHONE[1])                                             # the stored number is untouched…
        n0 = len(msgs(eng, 1, "outbound"))
        eng.advance(days=3); eng.tick(); eng.advance(days=3); eng.tick()
        self.assertEqual(len([m for m in msgs(eng, 1, "outbound") if m["status"] == "sent"]), len([m for m in msgs(eng, 1, "outbound")[:n0] if m["status"] == "sent"]))   # …and not contacted
        later(eng); eng.receive_feed(ofeed(eng, [order("P-01", "ORD-1001Y", PHONE[1], name=p1["display_name"])]))
        self.assertIsNone(FI.patient_held(eng.conn, p1))

    def test_too_many_bad_rows_hold_the_whole_file_and_identical_duplicates_do_not_count(self):
        eng = make_engine(); later(eng)
        recs = [order("P-%d" % i, "ORD-%d" % i, "bad-%d" % i) for i in range(100, 105)] + [order("P-105", "ORD-105", "+12075550105")]
        r = eng.receive_feed(ofeed(eng, recs))
        self.assertEqual(r["verdict"], "rejected"); self.assertIn("rows:orders", failed(r))
        self.assertIsNone(row(eng.conn, "SELECT 1 FROM patients WHERE source_patient_id='P-105'"))
        self.assertEqual(FI._health(eng.conn, "RIVERBEND", "orders")["status"], "blocked")
        eng2 = make_engine(); later(eng2)
        a = order("P-120", "ORD-120", "+12075550120")
        r = eng2.receive_feed(ofeed(eng2, [a, dict(a), dict(a), dict(a), dict(a)]))                     # five copies of one valid order
        self.assertEqual(r["verdict"], "accepted_with_warnings"); self.assertIn("duplicates:orders", warned(r)); self.assertNotIn("rows:orders", failed(r))
        self.assertEqual(row(eng2.conn, "SELECT COUNT(*) n FROM orders WHERE source_order_id='ORD-120'")["n"], 1)
        eng3 = make_engine(); later(eng3)
        b2 = order("P-120", "ORD-120", "+12075550120", ordered_at="2026-06-01T09:00:00")
        r = eng3.receive_feed(ofeed(eng3, [a, b2])); self.assertEqual(r["verdict"], "rejected"); self.assertIn("duplicates:orders", failed(r))

    def test_consent_column_absent_is_a_file_failure_and_missing_on_some_rows_is_row_level(self):
        eng = make_engine(); later(eng)
        a = order("P-110", "ORD-110", "+12075550110"); del a["patient"]["consent_sms"]
        b = order("P-111", "ORD-111", "+12075550111"); del b["patient"]["consent_sms"]
        r = eng.receive_feed(ofeed(eng, [a, b])); self.assertEqual(r["verdict"], "rejected"); self.assertIn("consent", failed(r))
        eng2 = make_engine(); later(eng2)
        c = order("P-112", "ORD-112", "+12075550112"); c["patient"]["consent_sms"] = "yes"
        r = eng2.receive_feed(ofeed(eng2, [c, order("P-113", "ORD-113", "+12075550113"), order("P-114", "ORD-114", "+12075550114"), order("P-115", "ORD-115", "+12075550115")]))
        self.assertEqual(r["verdict"], "accepted_with_warnings"); self.assertIsNone(row(eng2.conn, "SELECT 1 FROM patients WHERE source_patient_id='P-112'"))

    def test_encoding_order_age_state_coverage_unknown_codes_and_bad_enums(self):
        eng = make_engine(); later(eng)
        a = order("P-130", "ORD-130", "+12075550130", name="JosÃ© Ramirez", ordered_at="2023-01-01T09:00:00"); del a["state"]
        a["lines"] = [{"test_code": "ZZZ", "test_name": "Not a real test"}]
        r = eng.receive_feed(ofeed(eng, [a]))
        self.assertEqual(r["verdict"], "accepted_with_warnings")
        self.assertTrue({"encoding", "order_age", "state_coverage", "test_codes"} <= set(warned(r)), warned(r))
        for code in ("encoding", "order_age", "state_coverage", "test_codes"):
            al = alerts(eng, code); self.assertTrue(al); self.assertFalse(al[0]["partner_action_needed"]); self.assertIsNone(al[0]["notified_at"])
        later(eng)
        r = eng.receive_feed(ofeed(eng, [order("P-131", "ORD-131", "+12075550131", state="teleported"), order("P-132", "ORD-132", "+12075550132", lines=[{"test_code": "TSH", "test_name": "TSH", "status": "maybe"}]),
                                         order("P-133", "ORD-133", "+12075550133"), order("P-134", "ORD-134", "+12075550134")]))
        self.assertEqual(r["quarantined"], 2); self.assertIsNone(row(eng.conn, "SELECT 1 FROM orders WHERE source_order_id='ORD-131'"))

    def test_identifier_rekeying_counts_distinct_normalised_patients(self):
        eng = make_engine(); later(eng)
        recs = [order("P-%02d" % i, "ORD-RK-%02d" % i, "+1207555%04d" % (9000 + i), name="Person %d" % i) for i in range(1, 8)]   # 7 known patients, name AND phone new
        r = eng.receive_feed(ofeed(eng, recs)); self.assertEqual(r["verdict"], "rejected"); self.assertIn("identifiers", failed(r))
        self.assertEqual(patient(eng, 1)["phone"], PHONE[1])
        eng2 = make_engine(); later(eng2)
        recs = [order("P-01", "ORD-RK-%d" % k, "+12075559001", name="Person 1") for k in range(6)]                     # six orders for ONE re-keyed patient: below the minimum
        r = eng2.receive_feed(ofeed(eng2, recs)); self.assertNotEqual(r["verdict"], "rejected")
        eng3 = make_engine(); later(eng3)
        recs = [order("P-%02d" % i, "ORD-RK-%02d" % i, PHONE[i].replace("+1", "+1 "), name=patient(eng3, i)["display_name"].upper()) for i in range(1, 8)]   # formatting only
        r = eng3.receive_feed(ofeed(eng3, recs)); self.assertNotIn("identifiers", failed(r))

    def test_volume_drift_and_empty_file_warn_operator_only_after_a_baseline(self):
        eng = make_engine(policy=Policy(feed_baseline_min_files=2))
        for k in range(2):
            later(eng, 24)
            self.assertEqual(eng.receive_feed(ofeed(eng, [order("P-%d" % (200 + 10 * k + j), "ORD-%d" % (200 + 10 * k + j), "+1207555%04d" % (200 + 10 * k + j)) for j in range(8)]))["verdict"], "accepted")
        later(eng, 24)
        r = eng.receive_feed(ofeed(eng, [order("P-300", "ORD-300", "+12075550300")]))
        self.assertEqual(r["verdict"], "accepted_with_warnings"); self.assertIn("volume:orders", warned(r))
        a = alerts(eng, "volume")[-1]; self.assertFalse(a["partner_action_needed"]); self.assertIsNone(a["notified_at"])
        h = FI._health(eng.conn, "RIVERBEND", "orders"); self.assertEqual(h["baseline_n"], 3)                  # the anomalous file did not train the baseline (seed + two normal files)
        later(eng, 24)
        r = eng.receive_feed(ofeed(eng, [])); self.assertIn("empty orders file", [c["detail"] for c in r["checks"] if c["check"] == "volume:orders"][0])
        self.assertEqual(alerts(eng, "volume")[-1]["occurrences"], 2)
        self.assertEqual(FI._health(eng.conn, "RIVERBEND", "orders")["baseline_n"], 3)
        eng2 = make_engine(); later(eng2)
        r = eng2.receive_feed(ofeed(eng2, [])); self.assertIn("volume:orders", warned(r))                          # an empty orders file warns even before a baseline exists

    def test_result_events_are_validated_and_only_applied_results_count_as_results(self):
        eng = make_engine(); later(eng)
        r = eng.receive_feed(feed([{"source_order_id": "ORD-1001", "kind": "teleported", "at": eng.now().isoformat()},
                                   {"source_order_id": "ORD-1001", "kind": "result_finalized", "lines": ["NOTREAL"], "at": "2099-01-01T00:00:00"},     # future: quarantined
                                   {"source_order_id": "ORD-1001", "kind": "result_finalized", "lines": ["NOTREAL"], "at": eng.now().isoformat()},     # unknown code: importer rejects → held
                                   {"source_order_id": "ORD-1001", "kind": "attended", "at": eng.now().isoformat()},
                                   {"source_order_id": "ORD-1002", "kind": "attended", "at": eng.now().isoformat()},
                                   {"source_order_id": "ORD-1003", "kind": "attended", "at": eng.now().isoformat()},
                                   {"source_order_id": "ORD-1004", "kind": "attended", "at": eng.now().isoformat()}], eng.now()))
        self.assertEqual(r["verdict"], "accepted_with_warnings"); self.assertEqual(r["quarantined"], 3)
        self.assertIn("import", warned(r)); self.assertIn("rows:updates", warned(r))
        self.assertIsNone(FI._health(eng.conn, "RIVERBEND", "updates")["last_result_event_at"])         # nothing applied → the results clock did not move
        later(eng)
        r = eng.receive_feed(feed([{"source_order_id": "ORD-1001", "kind": "result_finalized", "lines": ["CBC"], "at": eng.now().isoformat()}], eng.now()))
        self.assertEqual(r["verdict"], "accepted"); self.assertEqual(FI._health(eng.conn, "RIVERBEND", "updates")["last_result_event_at"], eng.now().isoformat())


class FI_Block(unittest.TestCase):
    def _reject(self, eng, start=400):
        return eng.receive_feed(ofeed(eng, [order("P-%d" % i, "ORD-%d" % i, "bad") for i in range(start, start + 5)]))

    def test_queue_then_reject_then_flush_sends_nothing_scheduled(self):
        eng = make_engine(import_feed=True, tick=False)
        eng.tick()
        eng.advance(days=3)
        # queue the follow-ups without flushing them
        for c in rows(eng.conn, "SELECT * FROM conversations WHERE next_action_at IS NOT NULL AND next_action_at<=?", (eng.now().strftime("%Y-%m-%dT%H:%M:%S"),)):
            eng._do_outreach(c)
        queued = row(eng.conn, "SELECT COUNT(*) n FROM messages WHERE direction='outbound' AND status='queued'")["n"]; self.assertGreater(queued, 0)
        self.assertEqual(self._reject(eng)["verdict"], "rejected")
        out = eng._flush_outbox()                                                                         # before any tick composes the pause
        self.assertFalse(any(a.startswith("sent:") for a in out), out)
        self.assertEqual(row(eng.conn, "SELECT COUNT(*) n FROM messages WHERE direction='outbound' AND status='queued'")["n"], queued)
        eng.handle_inbound(PHONE[3], "STOP"); eng._flush_outbox()
        last = msgs(eng, 3, "outbound")[-1]; self.assertEqual((last["kind"], last["status"]), ("compliance", "sent"))   # compliance still leaves

    def test_held_file_pauses_offers_are_withheld_and_a_good_file_clears_it(self):
        eng = make_engine(); later(eng)
        self.assertEqual(self._reject(eng)["verdict"], "rejected")
        t = eng.tick(); self.assertIn("paused:feed_integrity", t["actions"]); self.assertEqual(eng.paused(), FI.PAUSE_REASON)
        self.assertTrue(eng.feed_integrity_blocked()); self.assertTrue(eng.feed_integrity_blocked("RIVERBEND"))
        later(eng)
        self.assertNotEqual(eng.receive_feed(ofeed(eng, [order("P-410", "ORD-410", "+12075550410")]))["verdict"], "rejected")
        t = eng.tick(); self.assertIn("resumed:feed_integrity_clear", t["actions"]); self.assertIsNone(eng.paused())

    def test_stale_to_fresh_while_blocked_leaves_no_one_tick_opening(self):
        eng = make_engine()
        eng.advance(hours=50); eng.tick(); self.assertEqual(eng.paused(), "policy:stale_feed")
        self.assertEqual(self._reject(eng, 420)["verdict"], "rejected")                                  # a bad orders file while stale
        eng.import_updates(feed([], eng.now()))                                                            # updates fresh again…
        later(eng); eng.receive_feed(ofeed(eng, [order("P-430", "ORD-430", "+12075550430")]))             # …orders fresh again, but was blocked in between? no: this clears it
        # so recreate: block after fresh
        self.assertEqual(self._reject(eng, 440)["verdict"], "rejected")
        eng.conn.execute("UPDATE settings SET value='policy:stale_feed' WHERE key='pause_reason'"); eng.conn.commit()
        t = eng.tick()
        self.assertEqual(eng.paused(), FI.PAUSE_REASON)
        self.assertFalse(any(a.startswith("sent:") for a in t["actions"]), t["actions"])

    def test_operator_resume_while_blocked_does_not_send_and_the_next_tick_repauses(self):
        eng = make_engine(); later(eng)
        self._reject(eng, 450); eng.tick(); self.assertEqual(eng.paused(), FI.PAUSE_REASON)
        eng.resume(actor="kate"); self.assertIsNone(eng.paused())
        eng.advance(days=3); t = eng.tick()
        self.assertFalse(any(a.startswith("sent:") for a in t["actions"]), t["actions"]); self.assertEqual(eng.paused(), FI.PAUSE_REASON)

    def test_operator_override_needs_a_named_actor_and_a_note_and_a_kate_pause_is_not_touched(self):
        eng = make_engine(); later(eng)
        self._reject(eng, 460)
        self.assertFalse(FI.unblock(eng, "RIVERBEND", "orders", "kate", "")["ok"]); self.assertFalse(FI.unblock(eng, "RIVERBEND", "orders", "operator", "note")["ok"])
        self.assertTrue(FI.unblock(eng, "RIVERBEND", "orders", "kate", "partner confirmed the export was a test run; real file due tomorrow")["ok"])
        self.assertIsNone(FI.blocked(eng)); self.assertEqual(FI._health(eng.conn, "RIVERBEND", "orders")["status"], "warning")
        eng.pause("manual", actor="kate")
        self._reject(eng, 470); eng.tick(); self.assertEqual(eng.paused(), "kate:manual")
        later(eng); eng.receive_feed(ofeed(eng, [order("P-480", "ORD-480", "+12075550480")])); eng.tick(); self.assertEqual(eng.paused(), "kate:manual")


class FI_Monitoring(unittest.TestCase):
    def test_late_file_is_noticed_at_any_delay_told_once_and_closed_on_arrival_then_rearmed(self):
        eng = make_engine()
        eng.advance(hours=60); t = eng.tick()                                                              # first look well past the window
        self.assertIn("feed_late:orders", t["actions"]); a = alerts(eng, "late"); self.assertEqual(len(a), 2)
        self.assertTrue(a[0]["notified_at"]); self.assertIn("export job", a[0]["message_to_partner"]); self.assertEqual(len(eng.tech_contact.sent), 2)
        eng.advance(hours=4); eng.tick(); self.assertEqual(len(alerts(eng, "late")), 2); self.assertEqual(len(eng.tech_contact.sent), 2)   # one incident, many observations
        refresh(eng); eng.tick()                                                                            # recovery closes both
        self.assertEqual([x["status"] for x in alerts(eng, "late")], ["resolved", "resolved"]); self.assertIn("recovered", alerts(eng, "late")[0]["resolution"])
        eng.advance(hours=31); eng.tick()                                                                   # second outage = new incident, new messages
        self.assertEqual(len(alerts(eng, "late")), 4); self.assertEqual(len(eng.tech_contact.sent), 4)

    def test_a_stream_that_never_started_is_late_too(self):
        eng = make_engine(import_feed=False)
        eng.import_orders(ofeed(eng, [order("P-500", "ORD-500", "+12075550500")]), source_name="first")   # orders only, never an updates file
        eng.advance(hours=31); t = eng.tick()
        self.assertIn("feed_late:updates", t["actions"]); self.assertEqual(alerts(eng, "late")[-1]["feed_kind"], "updates")

    def test_results_stream_silence_is_noticed_while_orders_keep_arriving_and_closes_when_results_resume(self):
        eng = make_engine(policy=Policy(feed_results_silent_days=5))
        for _ in range(6):
            eng.advance(hours=24); refresh(eng)
        t = eng.tick(); self.assertIn("feed_results_silent", t["actions"])
        a = alerts(eng, "results_silent")[-1]; self.assertTrue(a["partner_action_needed"]); self.assertIn("results", a["message_to_partner"].lower())
        later(eng); r = eng.receive_feed(feed([{"source_order_id": "ORD-1001", "kind": "result_finalized", "lines": ["CBC"], "at": eng.now().isoformat()}], eng.now())); self.assertEqual(r["verdict"], "accepted")
        self.assertEqual(alerts(eng, "results_silent")[-1]["status"], "resolved")

    def test_notification_failure_becomes_an_operator_item(self):
        eng = make_engine(); later(eng); eng.tech_contact.fail_times = 1
        eng.receive_feed(ofeed(eng, [order("P-510", "ORD-510", "bad"), order("P-511", "ORD-511", "+12075550511"), order("P-512", "ORD-512", "+12075550512"), order("P-513", "ORD-513", "+12075550513")]))
        a = alerts(eng, "rows_quarantined")[-1]; self.assertIsNone(a["notified_at"]); self.assertIn("simulated notification failure", a["notify_error"])
        nf = alerts(eng, "notify_failed"); self.assertEqual(len(nf), 1); self.assertFalse(nf[0]["partner_action_needed"])

    def test_acknowledge_and_resolve_need_a_named_person_and_resolve_needs_a_note(self):
        eng = make_engine(); later(eng)
        eng.receive_feed(ofeed(eng, [order("P-520", "ORD-520", "bad"), order("P-521", "ORD-521", "+12075550521"), order("P-522", "ORD-522", "+12075550522"), order("P-523", "ORD-523", "+12075550523")]))
        a = alerts(eng, "rows_quarantined")[-1]
        self.assertFalse(FI.acknowledge(eng, a["id"], "")["ok"]); self.assertFalse(FI.acknowledge(eng, a["id"], "operator")["ok"])
        self.assertTrue(FI.acknowledge(eng, a["id"], "kate", "looking")["ok"])
        self.assertEqual(row(eng.conn, "SELECT status, acknowledged_by, acknowledged_note FROM feed_alerts WHERE id=?", (a["id"],)), {"status": "acknowledged", "acknowledged_by": "kate", "acknowledged_note": "looking"})
        self.assertFalse(FI.resolve(eng, a["id"], "kate", "")["ok"]); self.assertFalse(FI.resolve(eng, a["id"], "", "fixed")["ok"])
        self.assertTrue(FI.resolve(eng, a["id"], "kate", "partner fixed the phone format")["ok"]); self.assertEqual(FI.summary(eng)["open_alerts"], 0)


class FI_Sources(unittest.TestCase):
    def test_inbox_pickup_claims_files_first_files_by_verdict_and_a_second_pass_is_an_idempotent_replay(self):
        d = tempfile.mkdtemp()
        try:
            eng = make_engine(); later(eng); eng.feed_source = FI.InboxFeedSource(d, settle_s=0, reclaim_after_s=0)
            json.dump(ofeed(eng, [order("P-600", "ORD-600", "+12075550600")]), open(os.path.join(d, "riverbend-orders-1.json"), "w"))
            open(os.path.join(d, "riverbend-orders-2.json"), "wb").write(b"\xff garbage")
            t = eng.tick()
            self.assertIn("feed_pulled:riverbend-orders-1.json:accepted", t["actions"]); self.assertIn("feed_pulled:riverbend-orders-2.json:rejected", t["actions"])
            self.assertTrue(os.path.exists(os.path.join(d, "processed", "riverbend-orders-1.json"))); self.assertTrue(os.path.exists(os.path.join(d, "quarantine", "riverbend-orders-2.json")))
            self.assertEqual(eng.feed_source.pending(), [])
            # a crash between commit and move: the claimed file is offered again; the second pass is a replay, not a second application
            shutil.copy(os.path.join(d, "processed", "riverbend-orders-1.json"), os.path.join(d, "riverbend-orders-1.json.claimed-deadbeef"))
            t = eng.tick(); self.assertIn("feed_pulled:riverbend-orders-1.json:accepted_replay", t["actions"])
            self.assertEqual(row(eng.conn, "SELECT COUNT(*) n FROM orders WHERE source_order_id='ORD-600'")["n"], 1)
            # a file still being written is left alone
            eng.feed_source.settle_s = 3600; json.dump(ofeed(eng, []), open(os.path.join(d, "fresh.json"), "w"))
            self.assertEqual(eng.feed_source.pending(), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_retained_payload_lets_a_rejected_push_be_inspected_and_reprocessed(self):
        eng = make_engine(); later(eng)
        r = eng.receive_feed(json.dumps(feed([{"source_order_id": "ORD-9999", "kind": "cancelled", "at": eng.now().isoformat()}], eng.now())).encode(), source="push", source_name="events.json")
        self.assertEqual(r["verdict"], "accepted_with_warnings")                                           # unknown order: deferred by the importer, listed as held
        self.assertTrue(any(q["source_id"] == "ORD-9999" for q in json.loads(row(eng.conn, "SELECT quarantined FROM feed_receipts WHERE id=?", (r["receipt_id"],))["quarantined"])))
        self.assertIsNotNone(row(eng.conn, "SELECT 1 FROM feed_payloads WHERE receipt_id=?", (r["receipt_id"],)))
        later(eng); eng.receive_feed(ofeed(eng, [order("P-700", "ORD-9999", "+12075550700")]))            # now the order exists
        h0 = FI._health(eng.conn, "RIVERBEND", "updates")
        rr = FI.reprocess(eng, r["receipt_id"], "kate"); self.assertTrue(rr["ok"]); self.assertTrue(rr["applied"]); self.assertIn("reprocess", [c["check"] for c in rr["checks"]])
        self.assertEqual(row(eng.conn, "SELECT state FROM orders WHERE source_order_id='ORD-9999'")["state"], "cancelled_by_partner")   # the deferred event applied on operator reprocess
        self.assertEqual(FI._health(eng.conn, "RIVERBEND", "updates")["accepted_files"], h0["accepted_files"])                        # health untouched
        self.assertFalse(FI.reprocess(eng, 1, "kate")["ok"]); self.assertFalse(FI.reprocess(eng, r["receipt_id"], "operator")["ok"])    # internal receipts keep no payload; named actor required
        # a reprocess of a file that also carries orders never rewrites demographics
        later(eng); p1 = patient(eng, 1)
        r2 = eng.receive_feed(json.dumps(ofeed(eng, [order("P-01", "ORD-1001R", "+12075559001", name=p1["display_name"])], updates=[{"source_order_id": "ORD-1002", "kind": "cancelled", "at": eng.now().isoformat()}])).encode(), source="push")
        later(eng); eng.receive_feed(ofeed(eng, [order("P-01", "ORD-1001S", "+12075559002", name=p1["display_name"])]))          # newer phone
        rr = FI.reprocess(eng, r2["receipt_id"], "kate"); self.assertTrue(rr["ok"])
        self.assertEqual(patient(eng, 1)["phone"], "+12075559002")

    def test_trusted_local_path_still_leaves_receipts_and_health(self):
        eng = make_engine()
        self.assertEqual(row(eng.conn, "SELECT COUNT(*) n FROM feed_receipts WHERE source='internal'")["n"], 2)
        h = {r["feed_kind"]: r for r in rows(eng.conn, "SELECT * FROM feed_health")}
        self.assertEqual(set(h), {"orders", "updates"}); self.assertEqual(h["orders"]["accepted_files"], 1); self.assertEqual(h["orders"]["status"], "ok")
        self.assertEqual(alerts(eng), []); self.assertIsNone(row(eng.conn, "SELECT 1 FROM feed_payloads"))


class FI_ThirdPass(unittest.TestCase):
    """Codex's minimal counterexamples from the verification (Sept 23), as regressions."""

    def test_v1_a_non_object_row_beside_a_valid_order_is_accepted_with_the_row_held_and_the_receipt_true(self):
        eng = make_engine(); later(eng)
        r = eng.receive_feed(ofeed(eng, [42, order("P-NEW", "ORD-NEW", "+12075559991"), order("P-NEW2", "ORD-NEW2", "+12075559992"), order("P-NEW3", "ORD-NEW3", "+12075559993")]))
        self.assertEqual(r["verdict"], "accepted_with_warnings"); self.assertTrue(r["applied"])
        self.assertTrue(row(eng.conn, "SELECT 1 FROM orders WHERE source_order_id='ORD-NEW'"))
        rc = row(eng.conn, "SELECT verdict, applied FROM feed_receipts WHERE id=?", (r["receipt_id"],)); self.assertEqual((rc["verdict"], rc["applied"]), ("accepted_with_warnings", 1))
        # and when hold processing itself fails, the import is rolled back with it (holds live inside the transaction)
        eng2 = make_engine(); later(eng2)
        real = FI.row
        def boom(conn, sql, params=()):
            if "FROM patients WHERE partner_id=? AND source_patient_id=?" in sql and "SELECT 1" in sql:
                raise RuntimeError("simulated failure inside hold processing")
            return real(conn, sql, params)
        FI.row = boom
        try:
            before = snapshot(eng2)
            r = eng2.receive_feed(ofeed(eng2, [order("P-01", "ORD-1001H", "bad", name=patient(eng2, 1)["display_name"]), order("P-NEW4", "ORD-NEW4", "+12075559994"), order("P-NEW5", "ORD-NEW5", "+12075559995"), order("P-NEW6", "ORD-NEW6", "+12075559996")]))
        finally:
            FI.row = real
        self.assertEqual(r["verdict"], "rejected"); self.assertFalse(r["applied"]); self.assertEqual(snapshot(eng2), before)

    def test_v2_replay_never_restores_old_demographics_or_clears_a_newer_hold(self):
        eng = make_engine(); later(eng)
        old = ofeed(eng, [order("P-01", "ORD-1001", PHONE[1], name=patient(eng, 1)["display_name"])])
        eng.receive_feed(old); later(eng)
        new = ofeed(eng, [order("P-01", "ORD-1001", "+12075559991", name=patient(eng, 1)["display_name"])]); new["orders"][0]["patient"]["consent_sms"] = False
        eng.receive_feed(new)
        self.assertEqual((patient(eng, 1)["consent_sms"], patient(eng, 1)["phone"]), (0, "+12075559991"))
        r = eng.receive_feed(old); self.assertEqual(r["verdict"], "accepted_replay"); self.assertFalse(r["applied"])
        self.assertEqual((patient(eng, 1)["consent_sms"], patient(eng, 1)["phone"]), (0, "+12075559991"))     # withdrawn consent and the new number stand
        later(eng)
        eng.receive_feed(ofeed(eng, [order("P-01", "ORD-1001", "bad", name=patient(eng, 1)["display_name"]), order("P-GOOD", "ORD-GOOD", "+12075559992"), order("P-GOOD2", "ORD-GOOD2", "+12075559993"), order("P-GOOD3", "ORD-GOOD3", "+12075559994")]))
        self.assertTrue(FI.patient_held(eng.conn, patient(eng, 1)))
        eng.receive_feed(old); self.assertTrue(FI.patient_held(eng.conn, patient(eng, 1)))                   # a replay clears nothing
        # an older file behind the current good one is held outright: it never overwrites newer fields either
        r = eng.receive_feed(ofeed(eng, [order("P-01", "ORD-1001Z", PHONE[1], name=patient(eng, 1)["display_name"])], at=eng.now() - timedelta(hours=3)))
        self.assertEqual(r["verdict"], "rejected"); self.assertEqual(patient(eng, 1)["phone"], "+12075559991")

    def test_finding3_every_non_safety_message_is_withheld_at_flush_without_a_global_pause(self):
        X = {"when": "Monday", "site_name": "Synthetic site", "site_address": "10 Synthetic St", "slot_1": "9:00 am", "slot_2": "10:00 am", "slot_time": "9:00 am", "confirmation_id": "SIM-1"}
        for tpl, extra in (("offer_slots", X), ("offer_slot_one", X), ("booking_confirmed", X)):
            eng = make_engine(); later(eng)
            ok = eng._queue(conv(eng, 1), tpl, "reply", "verification:%s" % tpl, extra=extra)
            self.assertTrue(ok, tpl)
            eng.receive_feed(ofeed(eng, [order("P-BAD", "ORD-BAD", "bad")]))
            self.assertTrue(eng.feed_integrity_blocked()); self.assertIsNone(eng.paused())                  # no global pause yet
            out = eng._flush_outbox()
            self.assertFalse(any(a.startswith("sent:") for a in out), (tpl, out))
        # a per-patient hold withholds the same messages for that patient only
        eng = make_engine(); later(eng)
        eng._queue(conv(eng, 1), "offer_slots", "reply", "verification:hold1", extra={"when": "Monday", "site_name": "Synthetic site", "slot_1": "9:00 am", "slot_2": "10:00 am"})
        eng._queue(conv(eng, 2), "offer_slots", "reply", "verification:hold2", extra={"when": "Monday", "site_name": "Synthetic site", "slot_1": "9:00 am", "slot_2": "10:00 am"})
        eng.receive_feed(ofeed(eng, [order("P-01", "ORD-1001X", "bad", name=patient(eng, 1)["display_name"]), order("P-H1", "ORD-H1", "+12075559981"), order("P-H2", "ORD-H2", "+12075559982"), order("P-H3", "ORD-H3", "+12075559983")]))
        self.assertFalse(eng.feed_integrity_blocked()); self.assertTrue(FI.patient_held(eng.conn, patient(eng, 1)))
        out = eng._flush_outbox()
        sent_convs = {int(a.split(":")[1]) for a in out if a.startswith("sent:")}
        self.assertNotIn(conv(eng, 1)["id"], {row(eng.conn, "SELECT conversation_id c FROM messages WHERE id=?", (m,))["c"] for m in sent_convs})
        self.assertIn(conv(eng, 2)["id"], {row(eng.conn, "SELECT conversation_id c FROM messages WHERE id=?", (m,))["c"] for m in sent_convs})
        # the one exception: a hold acknowledgement still goes to a held patient who wrote to us
        self.assertIsNone(eng._feed_gate(patient(eng, 1), "reply", "hold_ack")); self.assertIsNotNone(eng._feed_gate(patient(eng, 1), "reply", "offer_slots"))
        self.assertIsNone(eng._feed_gate(patient(eng, 1), "compliance", "opt_out_confirm")); self.assertIsNone(eng._feed_gate(patient(eng, 1), "safety", "emergency"))

    def test_finding8_no_value_from_a_malformed_envelope_or_row_reaches_the_partner_message(self):
        eng = make_engine(); later(eng)
        f = ofeed(eng, []); f["generated_at"] = "SYNTHETIC_PRIVATE_VALUE"
        eng.receive_feed(f, source_name="env.json")
        a = alerts(eng, "file_rejected")[-1]
        self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", a["message_to_partner"]); self.assertIn("unreadable timestamp", a["message_to_partner"])
        # a held row for a non-phone reason: hold set, reason names the field, message names id and field only
        p2 = patient(eng, 2); bad = order("P-02", "ORD-1002X", PHONE[2], name="SYNTHETIC_NAME_VALUE"); del bad["patient"]["consent_sms"]; bad["ordered_at"] = "SYNTHETIC_DATE_VALUE"
        r = eng.receive_feed(ofeed(eng, [bad, order("P-Q1", "ORD-Q1", "+12075559971"), order("P-Q2", "ORD-Q2", "+12075559972"), order("P-Q3", "ORD-Q3", "+12075559973")]), source_name="rows.json")
        self.assertEqual(r["verdict"], "accepted_with_warnings"); self.assertTrue(FI.patient_held(eng.conn, p2))
        m = alerts(eng, "rows_quarantined")[-1]["message_to_partner"]
        for leak in ("SYNTHETIC_NAME_VALUE", "SYNTHETIC_DATE_VALUE", PHONE[2]):
            self.assertNotIn(leak, m)
        for ok in ("ORD-1002X", "consent_sms", "ordered_at"):
            self.assertIn(ok, m)
        # a non-object row names nobody: nothing held, nothing crashes
        later(eng)
        r = eng.receive_feed(ofeed(eng, [None, "text", order("P-Q4", "ORD-Q4", "+12075559974"), order("P-Q5", "ORD-Q5", "+12075559975"), order("P-Q6", "ORD-Q6", "+12075559976"), order("P-Q7", "ORD-Q7", "+12075559977")]))
        self.assertEqual((r["verdict"], r["applied"], r["holds"]["held"]), ("accepted_with_warnings", True, 0))


class FI_FourthPass(unittest.TestCase):
    """Codex's Verification 2 counterexamples (Sept 23): V1 residual, V4 stale-only slot offers, V3 hold_ack under the automatic pause."""

    def test_v1_rejected_can_only_mean_nothing_was_committed_and_a_post_commit_error_is_not_a_rejection(self):
        # Codex's counterexample: a one-shot failure in the receipt write → the import is rolled back with it, so 'rejected' is true
        eng = make_engine(); later(eng)
        real = FI._write_receipt; calls = {"n": 0}
        def fail_once(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("synthetic receipt failure")
            return real(*a, **k)
        FI._write_receipt = fail_once
        try:
            r = eng.receive_feed(ofeed(eng, [order("P-NEW", "ORD-NEW", "+12075559991")]))
        finally:
            FI._write_receipt = real
        self.assertEqual((r["verdict"], r["applied"]), ("rejected", False))
        self.assertIsNone(row(eng.conn, "SELECT 1 FROM orders WHERE source_order_id='ORD-NEW'"))
        self.assertEqual(row(eng.conn, "SELECT applied FROM feed_receipts WHERE id=?", (r["receipt_id"],))["applied"], 0)
        # an error AFTER the commit (health) is reported as an operator item against the committed receipt; the verdict stays true
        eng2 = make_engine(); later(eng2)
        real_acc = FI._accepted
        def boom(*a, **k):
            raise RuntimeError("synthetic health failure")
        FI._accepted = boom
        try:
            r = eng2.receive_feed(ofeed(eng2, [order("P-NEW2", "ORD-NEW2", "+12075559992")]))
        finally:
            FI._accepted = real_acc
        self.assertEqual((r["verdict"], r["applied"]), ("accepted", True)); self.assertIn("synthetic health failure", r["finalisation_error"])
        self.assertTrue(row(eng2.conn, "SELECT 1 FROM orders WHERE source_order_id='ORD-NEW2'"))
        rc = row(eng2.conn, "SELECT verdict, applied FROM feed_receipts WHERE id=?", (r["receipt_id"],)); self.assertEqual((rc["verdict"], rc["applied"]), ("accepted", 1))
        a = alerts(eng2, "internal_error"); self.assertEqual(len(a), 1); self.assertIn("after the import committed", a[0]["message_internal"])
        self.assertEqual(row(eng2.conn, "SELECT COUNT(*) n FROM feed_receipts WHERE verdict='rejected'")["n"], 0)

    def test_v4_stale_only_feed_withholds_every_offer_template_derived_from_the_registry(self):
        from ocp import templates as T
        from ocp.engine import OFFER_TEMPLATES
        self.assertEqual(OFFER_TEMPLATES, T.offer_templates())
        for must in ("offer_slots", "offer_slot_one", "offer_sites", "offer_sites_constrained", "ask_day", "reminder", "plan_confirmed", "booking_confirmed"):
            self.assertIn(must, OFFER_TEMPLATES)
        for k, v in T.TEMPLATES.items():                                   # by construction: any text naming a site or a slot is an offer
            if "{site_" in v or "{slot_" in v:
                self.assertIn(k, OFFER_TEMPLATES, k)
        X = {"when": "Monday", "site_name": "Synthetic site", "site_address": "10 Synthetic St", "slot_1": "9:00 am", "slot_2": "10:00 am", "slot_time": "9:00 am", "confirmation_id": "SIM-1"}
        for tpl in ("offer_slots", "offer_slot_one"):
            eng = make_engine(); later(eng, 50)
            self.assertTrue(eng.feed_is_stale()); self.assertFalse(eng.feed_integrity_blocked()); self.assertIsNone(eng.paused())
            self.assertTrue(eng._queue(conv(eng, 1), tpl, "reply", "v4:%s" % tpl, extra=X))
            out = eng._flush_outbox()
            self.assertFalse(any(a.startswith("sent:") for a in out), (tpl, out))
            self.assertEqual(row(eng.conn, "SELECT status FROM messages WHERE dedupe_key=?", ("v4:%s" % tpl,))["status"], "queued")
            # an ordinary reply still leaves on a stale feed
            self.assertTrue(eng._queue(conv(eng, 2), "cost_ack", "reply", "v4:cost:%s" % tpl))
            self.assertEqual(row(eng.conn, "SELECT status FROM messages WHERE dedupe_key=?", ("v4:cost:%s" % tpl,))["status"], "queued")
            eng._flush_outbox()
            self.assertEqual(row(eng.conn, "SELECT status FROM messages WHERE dedupe_key=?", ("v4:cost:%s" % tpl,))["status"], "sent")

    def test_v3_hold_ack_leaves_under_the_automatic_integrity_pause_but_not_under_a_persons_pause(self):
        eng = make_engine(); later(eng)
        self.assertTrue(eng._queue(conv(eng, 1), "hold_ack", "reply", "v3:ack"))
        eng.receive_feed(ofeed(eng, [order("P-BAD", "ORD-BAD", "bad")]))
        eng.tick(); self.assertEqual(eng.paused(), FI.PAUSE_REASON)
        eng._flush_outbox()
        self.assertEqual(row(eng.conn, "SELECT status FROM messages WHERE dedupe_key='v3:ack'")["status"], "sent")
        # anything else stays held under the same automatic pause
        self.assertTrue(eng._queue(conv(eng, 2), "cost_ack", "reply", "v3:cost")); eng._flush_outbox()
        self.assertEqual(row(eng.conn, "SELECT status FROM messages WHERE dedupe_key='v3:cost'")["status"], "queued")
        # a person's pause stops the acknowledgement too
        eng2 = make_engine(); later(eng2)
        eng2._queue(conv(eng2, 1), "hold_ack", "reply", "v3:ack2"); eng2.pause("manual", actor="kate"); eng2._flush_outbox()
        self.assertEqual(row(eng2.conn, "SELECT status FROM messages WHERE dedupe_key='v3:ack2'")["status"], "queued")


class FI_Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = make_engine(); cls.eng.advance(hours=1)
        try:
            cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.eng))
        except OSError as e:
            raise unittest.SkipTest("cannot bind 127.0.0.1 in this environment (%s); HTTP tests skipped" % e)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _post(self, path, body: bytes, headers=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=5) as resp:
            return json.loads(resp.read().decode())

    def test_push_endpoint_validates_the_bytes_and_the_ops_surface_shows_it(self):
        good = json.dumps(ofeed(self.eng, [order("P-700", "ORD-700", "+12075550700")])).encode()
        st, r = self._post("/api/feed/receive", good, {"X-Feed-Name": "riverbend-orders-push.json"}); self.assertEqual((st, r["verdict"]), (200, "accepted"))
        for body, hdr in ((b"\xff\xfe", {"X-Feed-Name": "broken.json"}), (b"[1,2,3]", {}), (b"3", {})):
            st, r = self._post("/api/feed/receive", body, hdr); self.assertEqual((st, r["verdict"]), (200, "rejected"))   # outcomes, not 500s
        st, r = self._post("/api/import/updates", json.dumps({"partner_id": "RIVERBEND", "generated_at": self.eng.now().isoformat(), "somethingelse": []}).encode())
        self.assertEqual((st, r["verdict"]), (200, "rejected"))
        f = self._get("/api/feed")
        self.assertEqual(f["technical_contact"]["email"], "integration@example.invalid")
        self.assertTrue(any(rc["source_name"] == "riverbend-orders-push.json" for rc in f["receipts"]))
        self.assertFalse(f["blocked"]); self.assertTrue(any(a["code"] == "file_rejected" for a in f["open_alerts"]))
        self.assertIn("never outsource", f["principle"]); self.assertEqual(f["policy"]["feed_late_grace_hours"], 6); self.assertIn("at-least-once", f["source"]["note"])
        bad = json.dumps(ofeed(self.eng, [order("P-%d" % i, "ORD-%d" % i, "bad") for i in range(710, 715)])).encode()
        st, r = self._post("/api/feed/receive", bad, {"X-Feed-Name": "riverbend-orders-bad.json"}); self.assertEqual(r["verdict"], "rejected")
        f = self._get("/api/feed"); self.assertTrue(f["blocked"])
        o = ops_payload(self.eng); self.assertGreaterEqual(o["feed"]["files_rejected"], 4); self.assertTrue(o["feed"]["blocked"])
        aid = [a["id"] for a in f["open_alerts"] if a["code"] == "file_rejected"][0]
        st, r = self._post("/api/feed/alerts/%d/acknowledge" % aid, b"{}"); self.assertFalse(r["ok"])                        # no actor
        st, r = self._post("/api/feed/alerts/%d/acknowledge" % aid, json.dumps({"actor": "kate"}).encode()); self.assertTrue(r["ok"])
        st, r = self._post("/api/feed/unblock", json.dumps({"kind": "orders", "note": "n"}).encode()); self.assertFalse(r["ok"])             # no actor
        st, r = self._post("/api/feed/unblock", json.dumps({"kind": "orders", "actor": "kate", "note": "test run acknowledged by the partner"}).encode()); self.assertTrue(r["ok"])
        self.assertFalse(ops_payload(self.eng)["feed"]["blocked"])
        rid = [rc["id"] for rc in f["receipts"] if rc["source_name"] == "riverbend-orders-bad.json"][0]
        st, r = self._post("/api/feed/receipts/%d/reprocess" % rid, json.dumps({"actor": "kate"}).encode()); self.assertTrue(r["ok"]); self.assertIn("reprocess", [c["check"] for c in r["checks"]])
        self.assertIsNone(row(self.eng.conn, "SELECT 1 FROM patients WHERE source_patient_id='P-710'"))                                  # orders stream never re-applied


if __name__ == "__main__":
    unittest.main()
