"""Partner feed import.  Two feed kinds, both idempotent on partner+source ids and on event identity.

orders feed  : {"partner_id", "generated_at", "orders": [{source_order_id, patient:{...}, ordered_at,
                ordering_provider, priority, lines:[{test_code,test_name}]}]}
updates feed : {"partner_id", "generated_at", "updates": [{event_id?, source_order_id,
                kind: result_finalized|cancelled, at, lines:[test_code,...]}]}

Nothing here commits.  The engine wraps each import in one transaction so line facts and the
derived order state land together or not at all.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from .db import log_event, row


class FeedRejected(Exception):
    pass


def _check_feed(feed: Dict, now: datetime) -> None:
    for k in ("partner_id", "generated_at"):
        if not feed.get(k):
            raise FeedRejected("feed missing %s" % k)
    try:
        g = datetime.fromisoformat(feed["generated_at"])
    except (TypeError, ValueError):
        raise FeedRejected("generated_at is not an ISO timestamp")
    if g > now + timedelta(hours=1):
        raise FeedRejected("generated_at %s is in the future (now %s)" % (feed["generated_at"], now.isoformat()))


def normalize_feed(feed: Dict) -> Dict:
    """Accept the v5 partner envelope (top-level `patients` + `orders` keyed by `source_patient_id`, `order_events`) as well
    as the older nested form (V5-10).  Returns a feed in the nested form the importer consumes.  Unsupported envelopes are
    rejected rather than silently applied as no-ops."""
    f = dict(feed)
    if "patients" in f and "orders" in f:
        by_id = {p["source_patient_id"]: p for p in f.get("patients", [])}
        orders = []
        for o in f.get("orders", []):
            if "patient" in o:
                orders.append(o); continue
            p = by_id.get(o.get("source_patient_id"))
            if not p:
                raise FeedRejected("order %s names a patient not in the feed's patients list" % o.get("source_order_id"))
            oc = dict(o); oc["patient"] = p
            if isinstance(o.get("ordering_clinician"), dict) and not oc.get("ordering_provider"):
                oc["ordering_provider"] = o["ordering_clinician"].get("name")
            orders.append(oc)
        f["orders"] = orders
    if "order_events" in f and "updates" not in f:
        f["updates"] = list(f.get("order_events") or [])
    for k in ("orders", "updates"):
        if k in f and not isinstance(f[k], list):
            raise FeedRejected("%s must be a list" % k)
    if "orders" not in f and "updates" not in f:
        raise FeedRejected("feed carries neither orders nor updates/order_events; nothing to import")
    return f


def import_orders(conn: sqlite3.Connection, feed: Dict, now: datetime, source_name: str = "") -> Dict:
    feed = normalize_feed(feed)
    _check_feed(feed, now)
    partner = feed["partner_id"]
    synthetic = 1 if feed.get("synthetic") is True else 0
    created_p = updated_p = created_o = dup_o = rejected_o = 0
    for rec in feed.get("orders", []):
        p = rec["patient"]
        if not rec.get("lines"):
            rejected_o += 1
            log_event(conn, now.isoformat(), "partner_feed", "order_rejected",
                      detail={"source_order_id": rec.get("source_order_id"), "reason": "no order lines"})
            continue
        phone = p.get("phone") or None
        partner_consent = 1 if p.get("consent_sms") else 0
        number_suppressed = bool(phone) and row(conn, "SELECT 1 FROM suppressed_numbers WHERE phone=?", (phone,)) is not None
        existing = row(conn, "SELECT * FROM patients WHERE partner_id=? AND source_patient_id=?",
                       (partner, p["source_patient_id"]))
        if existing:
            pid = existing["id"]
            consent = 1 if (partner_consent and not existing["local_opt_out"] and not number_suppressed) else 0
            changes = {}
            for col, val in (("display_name", p["display_name"]), ("home_town", p.get("home_town")),
                             ("language", p.get("language", "en")), ("phone", phone),
                             ("partner_consent_sms", partner_consent), ("consent_sms", consent)):
                if existing[col] != val:
                    changes[col] = "changed"
            if changes:
                conn.execute("UPDATE patients SET display_name=?, home_town=?, language=?, phone=?, partner_consent_sms=?, "
                             "consent_sms=? WHERE id=?",
                             (p["display_name"], p.get("home_town"), p.get("language", "en"), phone, partner_consent,
                              consent, pid))
                updated_p += 1
                log_event(conn, now.isoformat(), "partner_feed", "patient_updated", patient_id=pid,
                          detail={"fields": sorted(changes), "local_opt_out_preserved": bool(existing["local_opt_out"])})
                if "phone" in changes:
                    n = conn.execute("UPDATE messages SET status='cancelled', last_error='recipient phone changed by partner' "
                                     "WHERE status='queued' AND direction='outbound' AND conversation_id IN "
                                     "(SELECT id FROM conversations WHERE patient_id=?)", (pid,)).rowcount
                    log_event(conn, now.isoformat(), "system", "queued_messages_cancelled", patient_id=pid,
                              detail={"n": n, "reason": "recipient phone changed"})
        else:
            consent = 1 if (partner_consent and not number_suppressed) else 0
            cur = conn.execute(
                "INSERT INTO patients(partner_id,source_patient_id,display_name,phone,language,consent_sms,"
                "partner_consent_sms,local_opt_out,home_town,synthetic,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (partner, p["source_patient_id"], p["display_name"], phone, p.get("language", "en"), consent,
                 partner_consent, 1 if number_suppressed else 0, p.get("home_town"), synthetic, now.isoformat()))
            pid = cur.lastrowid
            created_p += 1
            log_event(conn, now.isoformat(), "partner_feed", "patient_created", patient_id=pid,
                      detail={"source_patient_id": p["source_patient_id"], "number_suppressed": number_suppressed})
        dup = row(conn, "SELECT id FROM orders WHERE partner_id=? AND source_order_id=?",
                  (partner, rec["source_order_id"]))
        if dup:
            dup_o += 1
            log_event(conn, now.isoformat(), "partner_feed", "order_duplicate_ignored", patient_id=pid,
                      order_id=dup["id"], detail={"source_order_id": rec["source_order_id"]})
            continue
        oid = _insert_order(conn, partner, pid, rec, now)
        created_o += 1
    # Shared phone numbers: quarantine every patient on a number that maps to more than one person.
    for r in conn.execute("SELECT phone FROM patients WHERE phone IS NOT NULL GROUP BY phone HAVING COUNT(*)>1").fetchall():
        conn.execute("UPDATE patients SET phone_ambiguous=1 WHERE phone=?", (r["phone"],))
        log_event(conn, now.isoformat(), "system", "phone_ambiguous", detail={"phone_suffix": r["phone"][-4:]})
    conn.execute("INSERT INTO feed_imports(partner_id,feed_kind,generated_at,imported_at,record_count,source_name) "
                 "VALUES(?,?,?,?,?,?)", (partner, "orders", feed["generated_at"], now.isoformat(),
                                        len(feed.get("orders", [])), source_name))
    return {"patients_created": created_p, "patients_updated": updated_p, "orders_created": created_o,
            "orders_duplicate": dup_o, "orders_rejected": rejected_o}


def _insert_order(conn, partner: str, pid: int, rec: Dict, now: datetime) -> int:
    import json as _json
    prov = rec.get("ordering_provider") or ((rec.get("ordering_clinician") or {}).get("name") if isinstance(rec.get("ordering_clinician"), dict) else None)
    care = rec.get("care_team")
    if care is None and isinstance(rec.get("ordering_clinician"), dict):
        care = {"ordering_clinician": rec["ordering_clinician"], "episode_team": rec.get("episode_team")}
    cur = conn.execute(
        "INSERT INTO orders(partner_id,source_order_id,patient_id,ordered_at,intended_due_at,ordering_provider,priority,state,"
        "state_updated_at,created_at,visit_at,care_team) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (partner, rec["source_order_id"], pid, rec["ordered_at"], rec.get("intended_due_at"), prov,
         rec.get("priority", "routine"), "imported", now.isoformat(), now.isoformat(), rec.get("visit_at"), _json.dumps(care) if care else None))
    oid = cur.lastrowid
    for line in rec["lines"]:
        st = line.get("status") if line.get("status") in ("outstanding", "resulted", "cancelled") else "outstanding"
        conn.execute("INSERT OR IGNORE INTO order_lines(order_id,test_code,test_name,status,resulted_at) VALUES(?,?,?,?,?)",
                     (oid, line["test_code"], line["test_name"], st, rec.get("resulted_at") if st == "resulted" else None))
    # V5-3: the feed's own order state is authoritative at import; a cancelled or resulted order never enters outreach
    state = (rec.get("state") or "open").lower()
    if state in ("cancelled", "replaced"):
        conn.execute("UPDATE order_lines SET status='cancelled' WHERE order_id=? AND status='outstanding'", (oid,))
        conn.execute("UPDATE orders SET state=?, state_updated_at=? WHERE id=?", ("cancelled_by_partner" if state == "cancelled" else "replaced", now.isoformat(), oid))
    elif state == "resulted" or (rec["lines"] and all(l.get("status") == "resulted" for l in rec["lines"])):
        conn.execute("UPDATE order_lines SET status='resulted', resulted_at=COALESCE(resulted_at, ?) WHERE order_id=?", (rec.get("resulted_at") or now.isoformat(), oid))
        conn.execute("UPDATE orders SET state='verified_complete', state_updated_at=?, verified_at=? WHERE id=?", (now.isoformat(), now.isoformat(), oid))
    log_event(conn, now.isoformat(), "partner_feed", "order_imported", patient_id=pid, order_id=oid,
              detail={"source_order_id": rec["source_order_id"], "ordered_at": rec["ordered_at"], "feed_state": state})
    record_order_event(conn, oid, pid, None, "imported", rec["ordered_at"], "partner_feed", {"lines": [l["test_code"] for l in rec["lines"]], "feed_state": state}, now)
    return oid


def record_order_event(conn, order_id: int, patient_id: int, event_id, kind: str, at, actor: str, detail: Dict, now: datetime) -> None:
    """Append-only longitudinal order history (v5).  Nothing here changes an order's state; the engine does that."""
    import json as _json
    conn.execute("INSERT INTO order_events(order_id,patient_id,event_id,kind,at,actor,detail,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                 (order_id, patient_id, event_id, kind, at, actor, _json.dumps(detail or {}, default=str), now.isoformat()))


def _event_id(partner: str, upd: Dict) -> str:
    if upd.get("event_id"):
        return "%s:%s" % (partner, upd["event_id"])
    import json as _json
    # V5-3: the derived id covers the FULL canonical payload, so two distinct events at the same time never collide
    canonical = _json.dumps({k: v for k, v in upd.items() if k != "event_id"}, sort_keys=True, default=str)
    h = hashlib.sha1(("%s|%s" % (partner, canonical)).encode()).hexdigest()[:20]
    return "%s:derived:%s" % (partner, h)


def apply_updates(conn: sqlite3.Connection, feed: Dict, now: datetime, source_name: str = "") -> List[Tuple[int, str]]:
    """Records line-level facts and returns [(order_id, change)] for the engine to apply transitions.
    Rejects ambiguous evidence: empty line lists, unknown test codes, unknown orders, replayed events."""
    feed = normalize_feed(feed)
    _check_feed(feed, now)
    partner = feed["partner_id"]
    changes: List[Tuple[int, str]] = []
    for upd in feed.get("updates", []):
        eid = _event_id(partner, upd)
        if row(conn, "SELECT 1 FROM feed_events WHERE event_id=?", (eid,)):
            log_event(conn, now.isoformat(), "partner_feed", "update_duplicate_ignored",
                      detail={"event_id": eid, "source_order_id": upd.get("source_order_id")})
            continue
        o = row(conn, "SELECT * FROM orders WHERE partner_id=? AND source_order_id=?", (partner, upd.get("source_order_id")))
        if not o:
            # Not recorded as consumed: a result that arrives before its order applies when replayed later.
            log_event(conn, now.isoformat(), "partner_feed", "update_for_unknown_order_pending",
                      detail={"source_order_id": upd.get("source_order_id"), "event_id": eid,
                              "note": "event not consumed; replay after the order is imported"})
            continue
        conn.execute("INSERT INTO feed_events(event_id,partner_id,source_order_id,kind,at,received_at) VALUES(?,?,?,?,?,?)",
                     (eid, partner, upd.get("source_order_id", ""), upd.get("kind", ""), upd.get("at"), now.isoformat()))
        kind = upd.get("kind")
        # V5-3: chronology — an event older than the newest already-applied structural event for this order is recorded
        # but not applied ("newest authorized event wins")
        latest = row(conn, "SELECT MAX(at) a FROM order_events WHERE order_id=? AND kind IN ('modified','cancelled','replaced','result_finalized')", (o["id"],)) or {}
        record_order_event(conn, o["id"], o["patient_id"], eid, kind or "unknown", upd.get("at"), upd.get("actor") or "partner_feed",
                           {k: v for k, v in upd.items() if k not in ("replacement",)}, now)
        if kind in ("modified", "cancelled", "replaced") and latest.get("a") and upd.get("at") and upd["at"] < latest["a"]:
            log_event(conn, now.isoformat(), "partner_feed", "update_out_of_order", order_id=o["id"], patient_id=o["patient_id"],
                      detail={"event_id": eid, "kind": kind, "at": upd.get("at"), "newest_applied": latest["a"], "note": "recorded in history; not applied"})
            continue
        if kind == "replaced":
            # the provider issued a new order that supersedes this one: import the replacement in the same feed, mark the old one
            rep = upd.get("replacement")
            if not isinstance(rep, dict) or not rep.get("lines") or not rep.get("source_order_id"):
                log_event(conn, now.isoformat(), "partner_feed", "update_rejected", order_id=o["id"], patient_id=o["patient_id"],
                          detail={"event_id": eid, "reason": "replaced without a replacement order (source_order_id + lines)"})
                continue
            existing = row(conn, "SELECT id FROM orders WHERE partner_id=? AND source_order_id=?", (partner, rep["source_order_id"]))
            new_id = existing["id"] if existing else _insert_order(conn, partner, o["patient_id"], dict(rep, ordering_provider=rep.get("ordering_provider") or o["ordering_provider"]), now)
            conn.execute("UPDATE orders SET superseded_by=? WHERE id=?", (new_id, o["id"]))
            conn.execute("UPDATE order_lines SET status='cancelled' WHERE order_id=? AND status='outstanding'", (o["id"],))
            record_order_event(conn, new_id, o["patient_id"], eid, "added", upd.get("at"), upd.get("actor") or "partner_feed", {"replaces": o["source_order_id"]}, now)
            changes.append((o["id"], "replaced"))
            changes.append((new_id, "screen"))
            continue
        if kind == "modified":
            adds = upd.get("add_lines") or []
            removes = upd.get("remove_lines") or []
            for l in adds:
                conn.execute("INSERT OR IGNORE INTO order_lines(order_id,test_code,test_name) VALUES(?,?,?)", (o["id"], l["test_code"], l["test_name"]))
            for code in removes:
                conn.execute("UPDATE order_lines SET status='cancelled' WHERE order_id=? AND test_code=? AND status='outstanding'", (o["id"], code))
            outstanding = row(conn, "SELECT COUNT(*) n FROM order_lines WHERE order_id=? AND status='outstanding'", (o["id"],))["n"]
            changes.append((o["id"], "modified" if outstanding else "cancelled_by_partner"))
            continue
        if kind == "attended":
            changes.append((o["id"], "attended"))
            continue
        if kind == "result_finalized":
            codes = upd.get("lines")
            known = {r["test_code"] for r in conn.execute("SELECT test_code FROM order_lines WHERE order_id=?", (o["id"],))}
            if not isinstance(codes, list) or not codes:
                log_event(conn, now.isoformat(), "partner_feed", "update_rejected", order_id=o["id"],
                          patient_id=o["patient_id"], detail={"event_id": eid, "reason": "result_finalized without an explicit non-empty line list"})
                continue
            unknown = [c for c in codes if c not in known]
            if unknown:
                log_event(conn, now.isoformat(), "partner_feed", "update_rejected", order_id=o["id"],
                          patient_id=o["patient_id"], detail={"event_id": eid, "reason": "unknown test codes", "codes": unknown})
                continue
            for code in codes:
                conn.execute("UPDATE order_lines SET status='resulted', resulted_at=? WHERE order_id=? AND test_code=? "
                             "AND status='outstanding'", (upd.get("at", now.isoformat()), o["id"], code))
            outstanding = row(conn, "SELECT COUNT(*) n FROM order_lines WHERE order_id=? AND status='outstanding'",
                              (o["id"],))["n"]
            log_event(conn, now.isoformat(), "partner_feed", "result_finalized", patient_id=o["patient_id"],
                      order_id=o["id"], detail={"event_id": eid, "lines": codes, "outstanding_after": outstanding, "at": upd.get("at")})
            changes.append((o["id"], "verified_complete" if outstanding == 0 else "partial_result"))
        elif kind == "cancelled":
            conn.execute("UPDATE order_lines SET status='cancelled' WHERE order_id=? AND status='outstanding'", (o["id"],))
            log_event(conn, now.isoformat(), "partner_feed", "order_cancelled", patient_id=o["patient_id"],
                      order_id=o["id"], detail={"event_id": eid, "at": upd.get("at")})
            changes.append((o["id"], "cancelled_by_partner"))
        else:
            log_event(conn, now.isoformat(), "partner_feed", "update_unknown_kind", order_id=o["id"],
                      detail={"event_id": eid, "kind": kind})
    conn.execute("INSERT INTO feed_imports(partner_id,feed_kind,generated_at,imported_at,record_count,source_name) "
                 "VALUES(?,?,?,?,?,?)", (partner, "updates", feed["generated_at"], now.isoformat(),
                                        len(feed.get("updates", [])), source_name))
    return changes
