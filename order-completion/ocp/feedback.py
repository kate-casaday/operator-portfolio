"""Kate's feedback loop: flag a response → capture evidence → record interpretation → export a task.

Two halves, kept apart on purpose:
  * EVIDENCE is captured automatically at flag time from the database and the running configuration
    (thread up to the flagged message, active preferences, orders, workflow state, the decision record that
    produced the flagged message, adapter/template/policy configuration, software version).
  * INTERPRETATION is what Kate typed: label (defect | preference | question), what should have happened,
    notes.  It is expert input to evaluate, not proof of the right behavior.

Export refuses unless every patient in the evidence is marked synthetic.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from datetime import datetime
from typing import Dict, List, Optional

from . import __version__, templates
from .db import row, rows, log_event, ensure_tables

LABELS = ("defect", "preference", "question")
STATUSES = ("open", "linked", "verified", "dismissed")
ISO = "%Y-%m-%dT%H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  conversation_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  label TEXT NOT NULL,                 -- defect | preference | question
  should_have TEXT NOT NULL,           -- Kate's plain-English expected behavior (interpretation)
  notes TEXT,                          -- Kate's notes (interpretation)
  evidence TEXT NOT NULL,              -- JSON, captured automatically
  status TEXT NOT NULL DEFAULT 'open', -- open | linked | verified | dismissed
  linked_ref TEXT,                     -- commit / test / task reference once a change is made
  verification_note TEXT,              -- what verified it (test name, review turn) or why dismissed
  archived INTEGER NOT NULL DEFAULT 0,  -- 1 after a fresh session reset: joins to live rows are meaningless, evidence JSON is authoritative
  updated_at TEXT
);
"""


def ensure_schema(conn) -> None:
    ensure_tables(conn, SCHEMA)          # never executescript mid-transaction (it commits)


def git_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=os.path.dirname(os.path.abspath(__file__)),
                                      stderr=subprocess.DEVNULL, text=True, timeout=3).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--", "."], cwd=os.path.dirname(os.path.abspath(__file__)),
                                        stderr=subprocess.DEVNULL, text=True, timeout=3).strip()
        return out + ("+dirty" if dirty else "")
    except Exception:  # noqa: BLE001
        return "unknown"


def _policy_snapshot(policy) -> Dict:
    out = {}
    for f in dataclasses.fields(policy):
        v = getattr(policy, f.name)
        out[f.name] = v.strftime("%H:%M") if hasattr(v, "strftime") else v
    return out


def capture_evidence(engine, message_id: int) -> Dict:
    m = row(engine.conn, "SELECT * FROM messages WHERE id=?", (message_id,))
    if not m:
        raise ValueError("no such message")
    cid = m["conversation_id"]
    conv = row(engine.conn, "SELECT c.*, p.display_name, p.phone, p.home_town, p.language, p.consent_sms, p.partner_id, "
                            "p.source_patient_id, p.synthetic FROM conversations c JOIN patients p ON p.id=c.patient_id WHERE c.id=?", (cid,))
    thread = rows(engine.conn, "SELECT id, direction, kind, status, template_id, body, decision, created_at, sent_at FROM messages "
                               "WHERE conversation_id=? AND id<=? ORDER BY id", (cid, message_id))
    for t in thread:
        if t.get("decision"):
            try:
                t["decision"] = json.loads(t["decision"])
            except (TypeError, ValueError):
                pass
    prefs = rows(engine.conn, "SELECT key, value, source, corrected, message_id, created_at FROM preferences "
                              "WHERE patient_id=? AND superseded_by IS NULL ORDER BY key", (conv["patient_id"],)) \
        if _table_exists(engine.conn, "preferences") else []
    pref_history = rows(engine.conn, "SELECT key, value, source, corrected, superseded_by, message_id, created_at FROM preferences "
                                     "WHERE patient_id=? ORDER BY id", (conv["patient_id"],)) if _table_exists(engine.conn, "preferences") else []
    orders = rows(engine.conn, "SELECT o.id, o.source_order_id, o.state, o.ordered_at, o.intended_due_at, o.verified_at, o.claim_location, "
                               "o.claim_in_network, o.state_updated_at, (SELECT GROUP_CONCAT(l.test_name||':'||l.status) FROM order_lines l "
                               "WHERE l.order_id=o.id) lines FROM orders o WHERE o.patient_id=? ORDER BY o.id", (conv["patient_id"],))
    escs = rows(engine.conn, "SELECT id, reason, queue, status, assigned_to, handoff_status, due_at, overdue, opened_at, resolved_at, resolution "
                             "FROM escalations WHERE conversation_id=? ORDER BY id", (cid,))
    events = rows(engine.conn, "SELECT at, actor, kind, detail FROM events WHERE conversation_id=? ORDER BY id", (cid,))
    latest_feed = row(engine.conn, "SELECT MAX(generated_at) g FROM feed_imports WHERE partner_id=?", (conv["partner_id"],)) or {}
    decision = None
    if m.get("decision"):
        try:
            decision = json.loads(m["decision"])
        except (TypeError, ValueError):
            decision = m["decision"]
    now = engine.now()
    return {
        "captured_at": now.strftime(ISO),
        "software": {"ocp_version": __version__, "git_commit": git_commit()},
        "configuration": {
            "model_adapter": getattr(engine.model, "name", "?"),
            "model_id": getattr(engine.model, "model", getattr(getattr(engine.model, "primary", None), "model", "mock-rules-v1")),
            "model_simulated": bool(getattr(engine.model, "simulated", True)),
            "messaging_adapter": getattr(engine.messaging, "name", "?"),
            "messaging_simulated": bool(getattr(engine.messaging, "simulated", True)),
            "policy": _policy_snapshot(engine.policy),
            "overdue_threshold_days": engine.policy.min_order_age_days,
            "directory_partner": engine.directory.partner_name,
            "directory_sites_valid_now": [s["id"] for s in engine.directory.sites(now)],
            "directory_records_now": {"sites": engine.directory.sites(now), "instructions": engine.directory._instructions},
            "directory_sites_rejected_now": engine.directory.rejected(now),
            "template_id": m.get("template_id"),
            "template_text": templates.TEMPLATES.get(m.get("template_id") or "", None),
        },
        "patient": {"synthetic": bool(conv.get("synthetic", 0)), "source_patient_id": conv["source_patient_id"], "partner_id": conv["partner_id"],
                    "display_name": conv["display_name"], "home_town": conv["home_town"], "language": conv["language"],
                    "consent_sms": conv["consent_sms"]},
        "conversation": {"id": cid, "state": conv["state"], "next_action": conv["next_action"], "next_action_at": conv["next_action_at"],
                         "next_action_reason": conv.get("next_action_reason"), "outreach_attempts": conv["outreach_attempts"],
                         "episode": conv.get("episode"), "epoch": conv["epoch"], "agreed_site_id": conv["agreed_site_id"],
                         "agreed_when": conv["agreed_when"], "agreed_date": conv["agreed_date"], "pending_site_id": conv["pending_site_id"],
                         "model_calls": conv["model_calls"], "outbound_count": conv["outbound_count"], "inbound_count": conv["inbound_count"]},
        "flagged_message": {"id": m["id"], "direction": m["direction"], "kind": m["kind"], "status": m["status"],
                            "template_id": m["template_id"], "body": m["body"], "decision": decision, "created_at": m["created_at"]},
        "thread_through_flagged_message": thread,
        "preferences_active": prefs,
        "preferences_history": pref_history,
        "orders": orders,
        "escalations": escs,
        "partner_feed": {"latest_generated_at": latest_feed.get("g"), "stale": engine.feed_is_stale(conv["partner_id"])},
        "sim_clock": now.strftime(ISO),
        "events_for_conversation": events,
    }


def _table_exists(conn, name) -> bool:
    return row(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)) is not None


def create(engine, message_id: int, label: str, should_have: str, notes: str = "", actor: str = "kate") -> Dict:
    ensure_schema(engine.conn)
    if label not in LABELS:
        raise ValueError("label must be one of %s" % (LABELS,))
    if not (should_have or "").strip():
        raise ValueError("'what should have happened' is required")
    ev = capture_evidence(engine, message_id)
    now = engine.now().strftime(ISO)
    cur = engine.conn.execute("INSERT INTO feedback(created_at,actor,conversation_id,message_id,label,should_have,notes,evidence,status,updated_at) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (now, actor, ev["conversation"]["id"], message_id, label, should_have.strip(), (notes or "").strip(),
                               json.dumps(ev, default=str), "open", now))
    fid = cur.lastrowid
    log_event(engine.conn, now, actor, "feedback_created", conversation_id=ev["conversation"]["id"],
              detail={"feedback_id": fid, "message_id": message_id, "label": label})
    engine.conn.commit()
    return get(engine, fid)


def get(engine, fid: int) -> Optional[Dict]:
    ensure_schema(engine.conn)
    f = row(engine.conn, "SELECT * FROM feedback WHERE id=?", (fid,))
    if f:
        f["evidence"] = json.loads(f["evidence"])
    return f


def list_all(engine) -> List[Dict]:
    ensure_schema(engine.conn)
    out = rows(engine.conn, "SELECT f.id, f.created_at, f.actor, f.conversation_id, f.message_id, f.label, f.should_have, f.notes, f.status, "
                            "f.linked_ref, f.verification_note, f.updated_at, f.archived, p.display_name, m.template_id, substr(m.body,1,90) body "
                            "FROM feedback f LEFT JOIN conversations c ON c.id=f.conversation_id LEFT JOIN patients p ON p.id=c.patient_id "
                            "LEFT JOIN messages m ON m.id=f.message_id ORDER BY f.id DESC")
    for r in out:
        if r["display_name"] is None or r["archived"]:
            ev = row(engine.conn, "SELECT evidence FROM feedback WHERE id=?", (r["id"],))
            try:
                e = json.loads(ev["evidence"])
                r["display_name"] = e["patient"]["display_name"] + " (archived session)"
                r["template_id"] = e["flagged_message"]["template_id"]; r["body"] = e["flagged_message"]["body"][:90]
            except Exception:  # noqa: BLE001
                r["display_name"] = "(archived)"
    return out


def update_status(engine, fid: int, status: str, linked_ref: str = "", verification_note: str = "", actor: str = "kate") -> Dict:
    """Kate's manually asserted status.  Minimum evidence: `linked` needs a reference, `verified` needs a
    verification note (and a reference, either new or already recorded); reopening is audited."""
    ensure_schema(engine.conn)
    if status not in STATUSES:
        raise ValueError("status must be one of %s" % (STATUSES,))
    cur = row(engine.conn, "SELECT * FROM feedback WHERE id=?", (fid,))
    if not cur:
        raise ValueError("no such feedback")
    if status == "linked" and not (linked_ref or "").strip():
        raise ValueError("'linked' requires a linked_ref (commit, test or task reference)")
    if status == "verified":
        if not (verification_note or "").strip():
            raise ValueError("'verified' requires a verification_note (what checked it)")
        if not ((linked_ref or "").strip() or cur["linked_ref"]):
            raise ValueError("'verified' requires a linked change reference")
    now = engine.now().strftime(ISO)
    engine.conn.execute("UPDATE feedback SET status=?, linked_ref=COALESCE(NULLIF(?, ''), linked_ref), "
                        "verification_note=COALESCE(NULLIF(?, ''), verification_note), updated_at=? WHERE id=?",
                        (status, linked_ref, verification_note, now, fid))
    f = row(engine.conn, "SELECT conversation_id FROM feedback WHERE id=?", (fid,))
    log_event(engine.conn, now, actor, "feedback_status", conversation_id=f["conversation_id"] if f else None,
              detail={"feedback_id": fid, "from": cur["status"], "status": status, "linked_ref": linked_ref,
                      "verification_note": verification_note, "reopened": cur["status"] in ("verified", "dismissed") and status == "open"})
    engine.conn.commit()
    return get(engine, fid)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"


def export_task(engine, fid: int, out_dir: Optional[str] = None) -> Dict:
    """Render a development task from one feedback item.  Synthetic-only guard."""
    f = get(engine, fid)
    if not f:
        raise ValueError("no such feedback")
    ev = f["evidence"]
    if not ev.get("patient", {}).get("synthetic"):
        raise PermissionError("export refused: evidence is not marked synthetic (patient.synthetic != 1)")
    thread_lines = []
    for t in ev["thread_through_flagged_message"]:
        who = "PATIENT" if t["direction"] == "inbound" else "APP (%s, %s)" % (t.get("template_id") or "-", t.get("status"))
        flag = "   <-- FLAGGED" if t["id"] == f["message_id"] else ""
        thread_lines.append("- %s: %s%s" % (who, t["body"], flag))
    prefs = ev.get("preferences_active") or []
    inbound_before = [t for t in ev["thread_through_flagged_message"] if t["direction"] == "inbound"]
    last_in = inbound_before[-1]["body"] if inbound_before else "(no inbound; scheduled message)"
    dec = ev["flagged_message"].get("decision")
    cfg = ev["configuration"]
    conv = ev["conversation"]
    criteria = [
        "Given the same synthetic conversation up to the flagged message (thread above), when the patient's last message is %r, the application's next outbound message and state change match: %s" % (last_in, f["should_have"]),
        "The change is expressed in application rules or templates (ocp/rules.py, ocp/engine.py, ocp/templates.py, ocp/directory.py), not in prompt wording alone; the model still only classifies intent.",
        "No path is opened that marks an order verified without a partner result, sends text outside quiet-hour/pause/consent rules, or offers an unverified site.",
        "A regression test in tests/ reproduces this synthetic thread (see stub) and asserts the new behavior; the full suite and `python3 -m ocp demo --quiet` still pass.",
        "Feedback #%d is set to `linked` with the commit reference, then `verified` with the test name once the test passes." % fid,
    ]
    if f["label"] == "question":
        criteria = ["This item is a QUESTION, not a change request: answer it in strategy/order-completion-prototype-review.md (or the README) and set the feedback item to `verified` with a pointer, or convert it to a defect/preference item."] + criteria[1:2]
    m = re.match(r"P-(\d+)$", ev["patient"]["source_patient_id"] or "")
    n_hint = str(int(m.group(1))) if m else "N"
    stub = '''```python
# tests/test_feedback_%d.py — reproduce feedback #%d (synthetic)
from tests.helpers import make_engine, msgs, conv, PHONE
def test_feedback_%d():
    eng = make_engine()            # synthetic cohort, sim clock 2026-09-15 10:00, initial outreach sent
    # replay the patient's messages in order (use the synthetic phone of source_patient_id %s):
%s
    last = msgs(eng, %s, "outbound")[-1]
    # assert the desired behavior here, e.g.:
    # assert last["template_id"] == "..."
    # assert conv(eng, %s)["state"] == "..."
```''' % (fid, fid, fid, ev["patient"]["source_patient_id"],
          "\n".join("    eng.handle_inbound(PHONE[%s], %r, %r)" % (n_hint, t["body"], "fb%d-%d" % (fid, i)) for i, t in enumerate(inbound_before)) or "    # (no inbound messages before the flagged one)",
          n_hint, n_hint)
    md = "\n".join([
        "# Development task from feedback #%d — %s" % (fid, f["label"].upper()),
        "",
        "> **SYNTHETIC DATA ONLY.**  Patient `%s` (partner `%s`) is a synthetic fixture.  This file was generated by the prototype's feedback export; it contains no real patient information." % (ev["patient"]["source_patient_id"], ev["patient"]["partner_id"]),
        "",
        "Created %s by %s · status `%s` · software ocp %s @ git %s · model `%s` via adapter `%s` (%s) · messaging `%s` (%s)" % (
            f["created_at"], f["actor"], f["status"], ev["software"]["ocp_version"], ev["software"]["git_commit"], cfg.get("model_id"),
            cfg["model_adapter"], "SIMULATED" if cfg["model_simulated"] else "LIVE", cfg["messaging_adapter"], "simulated" if cfg["messaging_simulated"] else "REAL"),
        "",
        "Two kinds of evidence are below: **response-time** facts recorded when the flagged message was produced (its decision record and snapshot), and **flag-time** context captured when Kate flagged it (state may have moved on since).",
        "",
        "## Kate's interpretation (expert input to evaluate, not proof of the right behavior)",
        "",
        "**Label:** %s" % f["label"],
        "",
        "**What should have happened:** %s" % f["should_have"],
        "",
        ("**Notes:** %s" % f["notes"]) if f["notes"] else "**Notes:** (none)",
        "",
        "## Evidence (captured automatically at flag time)",
        "",
        "### Conversation through the flagged message",
        "",
        *thread_lines,
        "",
        "### Why the application sent the flagged message (response-time decision record and configuration snapshot)",
        "",
        "```json", json.dumps(dec, indent=2, default=str) if dec else "(no decision record on this message)", "```",
        "",
        "### Known patient constraints and preferences (active; source shown)",
        "",
        *(["| key | value | source | corrected | from message |", "|---|---|---|---|---|"] +
          ["| %s | %s | %s | %s | %s |" % (p["key"], p["value"], p["source"], "yes" if p.get("corrected") else "", p.get("message_id") or "") for p in prefs]
          if prefs else ["(none recorded)"]),
        "",
        "### Constraint history (all statements, including corrected and withdrawn)",
        "",
        *(["| key | value | source | corrected | superseded | from message | at |", "|---|---|---|---|---|---|---|"] +
          ["| %s | %s | %s | %s | %s | %s | %s |" % (p["key"], p["value"], p["source"], "yes" if p.get("corrected") else "",
                                                    ("withdrawn" if p.get("superseded_by") == -1 else ("by #%s" % p["superseded_by"] if p.get("superseded_by") else "")),
                                                    p.get("message_id") or "", p.get("created_at")) for p in (ev.get("preferences_history") or [])]
          if ev.get("preferences_history") else ["(none)"]),
        "",
        "### Application state at flag time",
        "",
        "- Conversation: state `%s`, next action `%s` at `%s` — reason: %s" % (conv["state"], conv["next_action"], conv["next_action_at"], conv.get("next_action_reason") or "(not recorded)"),
        "- Plan: site `%s`, when `%s`, date `%s`; pending site `%s`; episode %s, epoch %s; attempts %s" % (
            conv["agreed_site_id"], conv["agreed_when"], conv["agreed_date"], conv["pending_site_id"], conv["episode"], conv["epoch"], conv["outreach_attempts"]),
        "- Orders: " + ("; ".join("%s [%s] %s ordered %s%s%s" % (o["source_order_id"], o["state"], o["lines"], (o.get("ordered_at") or "")[:10],
                                                                (" intended due %s" % o["intended_due_at"][:10]) if o.get("intended_due_at") else "",
                                                                (" claim: %s / in-network=%s" % (o["claim_location"], o["claim_in_network"])) if o.get("claim_location") else "") for o in ev["orders"]) or "(none)"),
        "- Escalations: " + ("; ".join("#%s %s → %s [%s, handoff=%s%s]" % (e["id"], e["reason"], e["queue"], e["status"], e.get("handoff_status"), ", OVERDUE" if e.get("overdue") else "") for e in ev["escalations"]) or "(none)"),
        "- Partner feed: latest `%s`, stale=%s · sim clock %s · overdue threshold %s days" % (ev["partner_feed"]["latest_generated_at"], ev["partner_feed"]["stale"], ev["sim_clock"], cfg["overdue_threshold_days"]),
        "- Directory valid now: %s; rejected: %s" % (cfg["directory_sites_valid_now"], cfg["directory_sites_rejected_now"]),
        "",
        "### Template that produced the flagged message",
        "",
        "`%s`: %s" % (cfg.get("template_id"), cfg.get("template_text")),
        "",
        "### Policy snapshot",
        "",
        "```json", json.dumps(cfg["policy"], indent=2), "```",
        "",
        "## Proposed acceptance criteria (drafted automatically; edit before assigning)",
        "",
        *["%d. %s" % (i + 1, c) for i, c in enumerate(criteria)],
        "",
        "## Regression test stub",
        "",
        stub,
        "",
        "## Verification record",
        "",
        "- Linked change: %s" % (f["linked_ref"] or "(none yet)"),
        "- Verification: %s" % (f["verification_note"] or "(none yet)"),
        "",
        "*The complete evidence JSON (including the %d audit events for this conversation) is attached as `%s` next to this file and stored in the application database (`feedback.evidence`, id %d).*" % (len(ev.get("events_for_conversation") or []), "task-%03d.evidence.json" % fid, fid),
    ])
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "feedback", "exports")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "task-%03d-%s.md" % (fid, _slug(f["should_have"])))
    with open(path, "w") as fh:
        fh.write(md)
    with open(os.path.join(out_dir, "task-%03d.evidence.json" % fid), "w") as fh:
        json.dump({"feedback_id": fid, "interpretation": {"label": f["label"], "should_have": f["should_have"], "notes": f["notes"]},
                   "evidence": ev}, fh, indent=1, default=str)
    log_event(engine.conn, engine.now().strftime(ISO), "kate", "feedback_exported", conversation_id=f["conversation_id"],
              detail={"feedback_id": fid, "path": path})
    engine.conn.commit()
    return {"path": path, "evidence_path": os.path.join(out_dir, "task-%03d.evidence.json" % fid), "markdown": md}
