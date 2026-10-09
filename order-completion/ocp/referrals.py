"""Version 5: clinical referrals — one record per handoff, with an explicit lifecycle and evidence (v5 brief §4).

"Sent" is never "received"; "received" is never "resolved".  A portal-link-only handoff is `offered` with delivery
`unverified` until evidence arrives.  This is an operational oversight surface, not a clinician inbox: the operator can
inspect, annotate, and follow up; resolving a clinical referral needs a basis and the appropriate authority (the
designated clinical reviewer, or partner response evidence).
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Dict, List, Optional

from .db import row, rows, log_event, ensure_tables
from .rules import add_business_hours, clinician_available

ISO = "%Y-%m-%dT%H:%M:%S"

KINDS = ("portal_link", "portal_relay", "clinician_queue", "emergency", "route_missing", "plan_change")
STATES = ("offered", "awaiting_patient", "sent", "queued", "acknowledged", "responded", "resolved", "failed", "expired", "cancelled")
DELIVERY = ("none", "unverified", "simulated", "confirmed", "failed")
TEAMS = ("ordering_clinician", "pcp", "episode_team", "nurse_line")
CLINICAL_KINDS = {"portal_link", "portal_relay", "clinician_queue", "emergency"}      # resolution needs clinical authority or partner evidence
DEFAULT_ROUTES = {"clinical_question": {"team": "ordering_clinician", "response_hours_business": 8, "channel": "portal_link"},
                  "staff_request": {"team": "nurse_line", "response_hours_business": 4, "channel": "portal_link"},
                  "emergency": {"team": "nurse_line", "response_hours_business": 1, "channel": "clinician_queue"},
                  "plan_change_reported": {"team": "ordering_clinician", "response_hours_business": 16, "channel": "clinician_queue"}}
URGENCY_DEFAULT = {"status": "pending_clinical_approval", "levels": {"emergency": {"response_hours_business": 1}, "priority": {"response_hours_business": 4}, "routine": {"response_hours_business": 8}}}

SCHEMA = """
CREATE TABLE IF NOT EXISTS referrals (
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  patient_id INTEGER NOT NULL,
  order_ids TEXT,                       -- JSON list of order ids
  kind TEXT NOT NULL,                   -- portal_link | portal_relay | clinician_queue | emergency | route_missing | plan_change
  reason TEXT NOT NULL,                 -- clinical_question | staff_request | emergency | plan_change_reported | ...
  topic TEXT,
  patient_words TEXT,                   -- the patient's original text, verbatim
  generated_summary TEXT,               -- clearly labeled; never the patient's words
  summary_method TEXT,                  -- rule | model
  receiving_team TEXT,                  -- ordering_clinician | pcp | episode_team | nurse_line
  receiving_name TEXT,
  routing_basis TEXT,
  routing_ambiguous INTEGER NOT NULL DEFAULT 0,
  urgency TEXT NOT NULL DEFAULT 'routine',
  urgency_policy_status TEXT NOT NULL,  -- pending_clinical_approval | approved
  state TEXT NOT NULL,
  delivery TEXT NOT NULL DEFAULT 'none',
  owner TEXT,
  next_action TEXT,
  next_action_at TEXT,
  response_due_at TEXT,
  overdue INTEGER NOT NULL DEFAULT 0,
  duplicate_of INTEGER,
  escalation_id INTEGER,
  portal_message_id INTEGER,
  missing_data TEXT,                    -- JSON list: e.g. ["order_rationale", "care_team"]
  outcome TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by TEXT,
  resolution_basis TEXT,
  resolution_authority TEXT
);
CREATE TABLE IF NOT EXISTS referral_events (
  id INTEGER PRIMARY KEY,
  referral_id INTEGER NOT NULL REFERENCES referrals(id),
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,                   -- created | offered | sent | delivery | acknowledged | responded | resolved | failed | note | duplicate | overdue | followup_task | refused
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_referrals_conv ON referrals(conversation_id);
"""


def ensure_schema(conn) -> None:
    ensure_tables(conn, SCHEMA)          # never executescript mid-transaction (it commits)


def _routes(engine) -> Dict:
    r = getattr(engine.directory, "escalation_routes", None) or {}
    return {"approved": bool(r.get("approved")), "routes": dict(DEFAULT_ROUTES, **(r.get("routes") or {}))}


def _urgency_policy(engine) -> Dict:
    return getattr(engine.directory, "urgency_policy", None) or URGENCY_DEFAULT


def route_for(engine, reason: str, order: Optional[Dict], patient: Dict) -> Dict:
    """Which team, by what basis, and whether the care-team record makes it ambiguous.  Ordering clinician, PCP and episode
    team are distinct; a missing or conflicting record is flagged, never guessed silently."""
    cfg = _routes(engine)
    rt = cfg["routes"].get(reason) or cfg["routes"]["clinical_question"]
    team = rt["team"]
    basis = "route[%s] → %s (%s)" % (reason, team, "partner-approved" if cfg["approved"] else "DEFAULT, pending partner approval")
    ambiguous, name = False, None
    ct = json.loads(order["care_team"]) if order and order.get("care_team") else {}
    if team == "ordering_clinician":
        name = (order or {}).get("ordering_provider") or ct.get("ordering_clinician", {}).get("name")
        if not name:
            ambiguous = True; basis += "; no ordering clinician on the order"
    elif team == "pcp":
        name = ct.get("pcp", {}).get("name") or (json.loads(patient.get("care_team") or "{}").get("pcp") or {}).get("name") if patient.get("care_team") else ct.get("pcp", {}).get("name")
        if not name:
            ambiguous = True; basis += "; no PCP on record"
    elif team == "episode_team":
        name = ct.get("episode_team", {}).get("name")
        if not name:
            ambiguous = True; basis += "; no episode team on the order"
    else:
        name = engine.directory.clinician_contact.get("name")
    if ct.get("ambiguous"):
        ambiguous = True; basis += "; care-team record marked ambiguous (%s)" % ct.get("ambiguous")
    return {"team": team, "name": name or "(unassigned)", "basis": basis, "ambiguous": ambiguous,
            "response_hours_business": float(rt.get("response_hours_business", 8)), "channel": rt.get("channel"), "approved": cfg["approved"]}


def _urgency(engine, reason: str, topic: Optional[str], kind: str) -> str:
    if kind == "emergency" or reason == "emergency":
        return "emergency"
    if topic in ("medication", "symptoms"):
        return "priority"
    return "routine"


def _summary(engine, reason: str, topic: Optional[str], patient: Dict, order: Optional[Dict]) -> str:
    """Rule-built, labeled generated summary.  A live model summary would be labeled `model`; not exercised."""
    cat = engine.catalog.category([r["test_code"] for r in rows(engine.conn, "SELECT l.test_code FROM order_lines l WHERE l.order_id=?", ((order or {}).get("id"),))]) if order else "lab testing"
    who = (order or {}).get("ordering_provider") or "the ordering clinician"
    return "Patient raised a %s%s about the %s ordered by %s." % (reason.replace("_", " "), (" (topic: %s)" % topic) if topic else "", cat, who)


def create(engine, conv: Dict, kind: str, reason: str, msg_id: Optional[int], topic: Optional[str] = None, state: str = "offered",
           delivery: str = "none", escalation_id: Optional[int] = None, portal_message_id: Optional[int] = None, extra: Optional[Dict] = None) -> int:
    ensure_schema(engine.conn)
    now = engine.now()
    patient = row(engine.conn, "SELECT * FROM patients WHERE id=?", (conv["patient_id"],))
    order = row(engine.conn, "SELECT * FROM orders WHERE patient_id=? AND state IN ('eligible','outreach_active','escalated','claimed_complete') ORDER BY id LIMIT 1", (patient["id"],))
    order_ids = [o["id"] for o in rows(engine.conn, "SELECT id FROM orders WHERE patient_id=? AND state IN ('eligible','outreach_active','escalated','claimed_complete')", (patient["id"],))]
    words = (row(engine.conn, "SELECT body FROM messages WHERE id=?", (msg_id,)) or {}).get("body") if msg_id else None
    route = route_for(engine, reason, order, patient)
    urgency = _urgency(engine, reason, topic, kind)
    pol = _urgency_policy(engine)
    hours = float((pol.get("levels", {}).get(urgency) or {}).get("response_hours_business") or route["response_hours_business"])
    due = add_business_hours(now, hours, engine.policy)
    missing = []
    from . import facts as _facts
    rat = _facts.rationale_for_reply(engine, patient["id"])
    if reason in ("clinical_question",) and not rat["documented"]:
        missing.append("order_rationale")
    if route["ambiguous"]:
        missing.append("care_team")
    dup = row(engine.conn, "SELECT id FROM referrals WHERE conversation_id=? AND kind=? AND reason=? AND state NOT IN ('resolved','failed','expired','cancelled')", (conv["id"], kind, reason))
    owner = {"portal_link": "patient → %s" % route["name"], "portal_relay": route["name"], "clinician_queue": route["name"], "emergency": route["name"],
             "route_missing": "kate", "plan_change": "kate"}.get(kind, "kate")
    next_action = {"portal_link": "await evidence from partner (unverified delivery)", "portal_relay": "await patient message" if state == "awaiting_patient" else "await partner acknowledgement",
                   "clinician_queue": "partner acknowledges the queue item", "emergency": "partner acknowledgement; next-day follow-up task",
                   "route_missing": "configure a clinical route with the partner", "plan_change": "reconcile with the partner's order events"}.get(kind, "")
    cur = engine.conn.execute("INSERT INTO referrals(conversation_id,patient_id,order_ids,kind,reason,topic,patient_words,generated_summary,summary_method,receiving_team,receiving_name,"
                              "routing_basis,routing_ambiguous,urgency,urgency_policy_status,state,delivery,owner,next_action,next_action_at,response_due_at,duplicate_of,escalation_id,"
                              "portal_message_id,missing_data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (conv["id"], patient["id"], json.dumps(order_ids), kind, reason, topic, words, _summary(engine, reason, topic, patient, order), "rule",
                               route["team"], route["name"], route["basis"], 1 if route["ambiguous"] else 0, urgency, pol.get("status", "pending_clinical_approval"), state, delivery,
                               owner, next_action, (now + timedelta(days=1)).strftime(ISO) if kind == "emergency" else None, due.strftime(ISO), dup["id"] if dup else None,
                               escalation_id, portal_message_id, json.dumps(missing), now.strftime(ISO), now.strftime(ISO)))
    rid = cur.lastrowid
    _event(engine, rid, "created", dict(kind=kind, reason=reason, state=state, delivery=delivery, route=route, urgency=urgency, policy=pol.get("status"), **(extra or {})))
    if dup:
        _event(engine, rid, "duplicate", {"of": dup["id"]})
    log_event(engine.conn, now.strftime(ISO), "system", "referral_created", conversation_id=conv["id"], patient_id=patient["id"],
              detail={"referral_id": rid, "kind": kind, "reason": reason, "team": route["team"], "ambiguous": route["ambiguous"], "duplicate_of": dup["id"] if dup else None})
    return rid


def _event(engine, rid: int, kind: str, detail: Optional[Dict] = None, actor: str = "system") -> None:
    engine.conn.execute("INSERT INTO referral_events(referral_id,at,actor,kind,detail) VALUES(?,?,?,?,?)",
                        (rid, engine.now().strftime(ISO), actor, kind, json.dumps(detail or {}, default=str)))


TERMINAL = {"resolved", "cancelled", "expired"}
# legal forward moves (V5-8); anything else is recorded as a late/illegal event and does not change state
LEGAL = {"offered": {"sent", "acknowledged", "responded", "failed", "expired", "cancelled"},
         "awaiting_patient": {"sent", "failed", "expired", "cancelled"},
         "sent": {"acknowledged", "responded", "failed", "cancelled"},
         "queued": {"acknowledged", "responded", "failed", "cancelled"},
         "acknowledged": {"responded", "failed", "cancelled"},
         "responded": {"failed", "cancelled"},
         "failed": {"sent", "queued", "cancelled"}}


def transition(engine, rid: int, state: str, actor: str = "system", delivery: Optional[str] = None, detail: Optional[Dict] = None, next_action: Optional[str] = None) -> bool:
    ensure_schema(engine.conn)
    assert state in STATES
    r = row(engine.conn, "SELECT * FROM referrals WHERE id=?", (rid,))
    if not r:
        return False
    now = engine.now().strftime(ISO)
    if state == "resolved":
        _event(engine, rid, "refused", {"actor": actor, "reason": "resolved is reached only through resolve() with a basis and authority"}, actor=actor)
        return False
    if r["state"] in TERMINAL:
        _event(engine, rid, "late_event", {"actor": actor, "attempted": state, "current": r["state"], "detail": detail or {}, "note": "terminal state preserved; reopen explicitly with a reason"}, actor=actor)
        engine.conn.execute("UPDATE referrals SET updated_at=? WHERE id=?", (now, rid))
        return False
    if state not in LEGAL.get(r["state"], set()) and state != r["state"]:
        _event(engine, rid, "illegal_transition", {"actor": actor, "attempted": state, "current": r["state"]}, actor=actor)
        return False
    engine.conn.execute("UPDATE referrals SET state=?, delivery=COALESCE(?, delivery), next_action=COALESCE(?, next_action), updated_at=? WHERE id=?",
                        (state, delivery, next_action, now, rid))
    _event(engine, rid, state, dict(detail or {}, **({"delivery": delivery} if delivery else {})), actor=actor)
    return True


def note(engine, rid: int, actor: str, text: str) -> None:
    _event(engine, rid, "note", {"text": text}, actor=actor)
    engine.conn.execute("UPDATE referrals SET updated_at=? WHERE id=?", (engine.now().strftime(ISO), rid))


def resolve(engine, rid: int, actor: str, role: str, basis: str, authority: str, outcome: str = "resolved") -> Dict:
    """A clinical referral (portal link / relay / queue / emergency) may be resolved only with (a) partner response evidence
    already recorded on it (`responded`), or (b) the designated clinical reviewer's authority.  The operator may resolve
    administrative kinds (route_missing, plan_change) with a basis.  Every attempt is recorded."""
    ensure_schema(engine.conn)
    r = row(engine.conn, "SELECT * FROM referrals WHERE id=?", (rid,))
    if not r:
        return {"ok": False, "reason": "no such referral"}
    if r["state"] == "resolved":
        return {"ok": False, "reason": "already resolved"}
    if not (basis or "").strip():
        _event(engine, rid, "refused", {"actor": actor, "reason": "no basis given"}, actor=actor)
        engine.conn.commit()
        return {"ok": False, "reason": "a basis is required"}
    if r["kind"] in CLINICAL_KINDS and not (role == "clinical_reviewer" or r["state"] == "responded"):
        _event(engine, rid, "refused", {"actor": actor, "role": role, "reason": "clinical referral: needs the designated clinical reviewer or recorded partner response evidence"}, actor=actor)
        engine.conn.commit()
        return {"ok": False, "reason": "clinical referral: operator cannot resolve without partner response evidence or clinical authority"}
    now = engine.now().strftime(ISO)
    engine.conn.execute("UPDATE referrals SET state='resolved', outcome=?, resolved_at=?, resolved_by=?, resolution_basis=?, resolution_authority=?, updated_at=?, next_action=NULL WHERE id=?",
                        (outcome, now, actor, basis, "%s:%s" % (role, authority), now, rid))
    _event(engine, rid, "resolved", {"basis": basis, "authority": authority, "role": role, "outcome": outcome}, actor=actor)
    engine.conn.commit()
    return {"ok": True}


def record_partner_evidence(engine, rid: int, kind: str, actor: str = "partner", detail: Optional[Dict] = None) -> Dict:
    """Evidence from the partner side: delivery confirmed, acknowledged, responded, failed.  In the prototype this is
    entered by hand or by a scenario step; in production it comes from the partner's system.  Labeled as such.
    V5-8: evidence is an artifact, not a label — `acknowledged` and `responded` need a source reference (the partner's
    message id, a portal thread reference, a call note id) and, for `responded`, a content summary."""
    ensure_schema(engine.conn)
    mapping = {"delivered": ("sent", "confirmed"), "acknowledged": ("acknowledged", None), "responded": ("responded", None), "failed": ("failed", "failed")}
    if kind not in mapping:
        return {"ok": False, "reason": "unknown evidence kind"}
    detail = dict(detail or {})
    if kind in ("acknowledged", "responded") and not (detail.get("source_ref") or "").strip():
        _event(engine, rid, "refused", {"actor": actor, "evidence": kind, "reason": "evidence needs a source reference (partner message id / portal thread / call note)"}, actor=actor)
        engine.conn.commit()
        return {"ok": False, "reason": "evidence needs a source_ref"}
    if kind == "responded" and not (detail.get("summary") or "").strip():
        _event(engine, rid, "refused", {"actor": actor, "evidence": kind, "reason": "a response needs a content summary"}, actor=actor)
        engine.conn.commit()
        return {"ok": False, "reason": "a response needs a summary"}
    state, delivery = mapping[kind]
    r = row(engine.conn, "SELECT state FROM referrals WHERE id=?", (rid,))
    if not r:
        return {"ok": False, "reason": "no such referral"}
    if kind == "delivered" and r["state"] not in ("offered", "sent", "queued"):
        state = r["state"]
    ok = transition(engine, rid, state, actor=actor, delivery=delivery, detail=dict(detail, evidence=kind, source="partner (entered by hand in the prototype)"))
    engine.conn.commit()
    return {"ok": ok, "state": state if ok else r["state"], "reason": None if ok else "state unchanged (terminal or illegal transition; see events)"}


def tick(engine) -> List[str]:
    """Overdue flags; relay offers that never got a message expire; emergency next-day follow-up tasks for the operator."""
    ensure_schema(engine.conn)
    now = engine.now()
    out: List[str] = []
    ns = now.strftime(ISO)
    for r in rows(engine.conn, "SELECT * FROM referrals WHERE state NOT IN ('resolved','failed','expired','cancelled') AND overdue=0 AND response_due_at IS NOT NULL AND response_due_at<=?", (ns,)):
        engine.conn.execute("UPDATE referrals SET overdue=1, updated_at=? WHERE id=?", (ns, r["id"]))
        _event(engine, r["id"], "overdue", {"due": r["response_due_at"], "note": "no acknowledgement or response evidence by the response window" if r["kind"] != "portal_link" else "portal-link handoff: delivery is unverified; no evidence arrived"})
        out.append("referral_overdue:%d" % r["id"])
    for r in rows(engine.conn, "SELECT * FROM referrals WHERE state='awaiting_patient' AND created_at<=?", ((now - timedelta(days=3)).strftime(ISO),)):
        transition(engine, r["id"], "expired", detail={"reason": "no patient message within 3 days of the relay offer"})
        out.append("referral_expired:%d" % r["id"])
    for r in rows(engine.conn, "SELECT * FROM referrals WHERE kind='emergency' AND next_action_at IS NOT NULL AND next_action_at<=? AND state NOT IN ('resolved','cancelled')", (ns,)):
        conv = row(engine.conn, "SELECT * FROM conversations WHERE id=?", (r["conversation_id"],))
        eid = engine._escalate(conv, "emergency_followup", "Next-day follow-up after emergency wording (referral #%d): confirm the partner acknowledged and the patient's status through the partner; do not text clinical content" % r["id"], None, keep_queued=True)
        engine.conn.execute("UPDATE referrals SET next_action='next-day follow-up task open (#%d)', next_action_at=NULL, updated_at=? WHERE id=?" % eid, (ns, r["id"]))
        _event(engine, r["id"], "followup_task", {"escalation_id": eid, "note": "separate step from the immediate 911 guidance; owned by a person"})
        out.append("emergency_followup_task:%d" % r["id"])
    return out


# ------------------------------------------------------------------------------------------------ payloads
def payload(engine, filters: Optional[Dict] = None) -> Dict:
    ensure_schema(engine.conn)
    f = filters or {}
    q = ("SELECT r.*, p.display_name, p.source_patient_id, p.partner_id FROM referrals r JOIN patients p ON p.id=r.patient_id WHERE 1=1")
    args: List = []
    for key, col in (("reason", "r.reason"), ("kind", "r.kind"), ("team", "r.receiving_team"), ("state", "r.state"), ("partner", "p.partner_id"), ("urgency", "r.urgency"), ("outcome", "r.outcome")):
        if f.get(key):
            q += " AND %s=?" % col; args.append(f[key])
    if f.get("missing"):
        q += " AND r.missing_data LIKE ?"; args.append("%%%s%%" % f["missing"])
    if f.get("overdue"):
        q += " AND r.overdue=1"
    if f.get("min_age_hours"):
        q += " AND r.created_at<=?"; args.append((engine.now() - timedelta(hours=float(f["min_age_hours"]))).strftime(ISO))
    items = rows(engine.conn, q + " ORDER BY r.overdue DESC, r.created_at DESC", args)
    now = engine.now()
    for r in items:
        r["age_hours"] = round((now - __import__("datetime").datetime.fromisoformat(r["created_at"])).total_seconds() / 3600.0, 1)
        r["missing_data"] = json.loads(r["missing_data"] or "[]")
        r["order_ids"] = json.loads(r["order_ids"] or "[]")
        r["delivery_label"] = {"none": "nothing sent by us", "unverified": "UNVERIFIED (portal link only; no evidence of delivery to the provider)",
                               "simulated": "simulated send (prototype)", "confirmed": "confirmed by partner evidence", "failed": "FAILED"}.get(r["delivery"], r["delivery"])
    by_state = {}
    for r in items:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    by_reason = {}
    for r in items:
        by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
    by_team = {}
    for r in items:
        by_team[r["receiving_team"]] = by_team.get(r["receiving_team"], 0) + 1
    return {"referrals": items, "by_state": by_state, "by_reason": by_reason, "by_team": by_team,
            "overdue": sum(1 for r in items if r["overdue"]), "duplicates": sum(1 for r in items if r["duplicate_of"]),
            "failed": sum(1 for r in items if r["state"] == "failed"), "ambiguous_routing": sum(1 for r in items if r["routing_ambiguous"]),
            "missing_rationale": sum(1 for r in items if "order_rationale" in r["missing_data"]),
            "routes": _routes(engine), "urgency_policy": _urgency_policy(engine),
            "escalation_criteria": getattr(engine.directory, "escalation_criteria", None) or {"status": "pending_clinical_approval"},
            "note": "operational oversight, not a clinician inbox; 'sent' is not 'received', 'received' is not 'resolved'; portal-link handoffs stay unverified until evidence arrives"}


def detail(engine, rid: int) -> Optional[Dict]:
    ensure_schema(engine.conn)
    r = row(engine.conn, "SELECT r.*, p.display_name, p.source_patient_id FROM referrals r JOIN patients p ON p.id=r.patient_id WHERE r.id=?", (rid,))
    if not r:
        return None
    r["missing_data"] = json.loads(r["missing_data"] or "[]"); r["order_ids"] = json.loads(r["order_ids"] or "[]")
    events = rows(engine.conn, "SELECT * FROM referral_events WHERE referral_id=? ORDER BY id", (rid,))
    for e in events:
        try:
            e["detail"] = json.loads(e["detail"] or "{}")
        except ValueError:
            pass
    from . import facts as _facts
    facts = _facts.rationale_for_reply(engine, r["patient_id"])
    orders = rows(engine.conn, "SELECT o.id, o.source_order_id, o.state, o.ordering_provider, (SELECT GROUP_CONCAT(l.test_name||':'||l.status) FROM order_lines l WHERE l.order_id=o.id) lines FROM orders o WHERE o.patient_id=?", (r["patient_id"],))
    thread = rows(engine.conn, "SELECT id, direction, body, template_id, status, created_at FROM messages WHERE conversation_id=? AND status NOT IN ('cancelled') ORDER BY id", (r["conversation_id"],))
    pm = row(engine.conn, "SELECT * FROM portal_messages WHERE id=?", (r["portal_message_id"],)) if r["portal_message_id"] else None
    esc = row(engine.conn, "SELECT * FROM escalations WHERE id=?", (r["escalation_id"],)) if r["escalation_id"] else None
    return {"referral": r, "events": events, "facts": facts, "orders": orders, "thread": thread, "portal_message": pm, "escalation": esc,
            "can_operator_resolve": r["kind"] not in CLINICAL_KINDS or r["state"] == "responded"}
