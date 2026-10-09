"""Version 5: feedback → reviewed evaluation cases → candidate changes with evaluation results, approval, release
history and rollback (v5 brief §7).

Three things are kept apart on purpose: interaction logs (messages/events tables), evaluation cases (this module +
eval/feedback_cases/), and any future training set (none exists; nothing here trains on conversations).  A candidate
change is a description of a proposed prompt/rule/template change plus the cases it must pass; releasing it is a
recorded decision, not an automatic deployment.  Model adapters stay interchangeable; a second model is a reviewer,
not proof.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

from .db import row, rows, log_event, ensure_tables
from . import feedback as fb

ISO = "%Y-%m-%dT%H:%M:%S"
CATEGORIES = ("tone", "factual_support", "operational_effectiveness", "routing", "longitudinal_order_handling")
TARGETS = ("message", "fact_card", "referral")
CASE_STATUSES = ("draft", "reviewed", "active", "retired")
CHANGE_STATUSES = ("proposed", "approved", "released", "rolled_back")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES_DIR = os.path.join(ROOT, "eval", "feedback_cases")

SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_cases (
  id INTEGER PRIMARY KEY,
  version INTEGER NOT NULL DEFAULT 1,
  supersedes_id INTEGER,
  source_feedback_id INTEGER,
  category TEXT NOT NULL,
  target_kind TEXT NOT NULL,
  title TEXT NOT NULL,
  case_json TEXT NOT NULL,              -- {setup: [...steps], expect: {...}} replayable on a fresh synthetic engine
  status TEXT NOT NULL DEFAULT 'draft', -- draft | reviewed | active | retired
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  reviewed_by TEXT,
  reviewed_at TEXT,
  exported_path TEXT
);
CREATE TABLE IF NOT EXISTS candidate_changes (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL,            -- what would change (prompt / rule / template / data), in words
  change_ref TEXT,                      -- commit / branch / file reference once it exists
  addresses TEXT NOT NULL,              -- JSON list of feedback ids
  eval_case_ids TEXT NOT NULL,          -- JSON list
  eval_results TEXT,                    -- JSON: {run_at, passed, failed, per_case: [...]} from the last run
  status TEXT NOT NULL DEFAULT 'proposed',
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  approved_by TEXT,
  approved_at TEXT,
  released_at TEXT,
  release_tag TEXT,
  rolled_back_at TEXT,
  rollback_reason TEXT
);
CREATE TABLE IF NOT EXISTS release_history (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  candidate_id INTEGER NOT NULL,
  action TEXT NOT NULL,                 -- approve | release | rollback
  tag TEXT,
  note TEXT
);
"""

FEEDBACK_MIGRATIONS = [("feedback", "target_kind", "TEXT NOT NULL DEFAULT 'message'"), ("feedback", "target_id", "INTEGER"), ("feedback", "category", "TEXT")]


def ensure_schema(conn) -> None:
    fb.ensure_schema(conn)
    ensure_tables(conn, SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(feedback)").fetchall()}
    for table, col, decl in FEEDBACK_MIGRATIONS:
        if col not in have:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
    conn.commit()


# ------------------------------------------------------------------------------------------------ feedback on any target
def flag(engine, target_kind: str, target_id: int, label: str, category: str, should_have: str, notes: str = "", actor: str = "kate") -> Dict:
    """Flag a message (the v3 path, evidence captured), a fact card, or a referral."""
    ensure_schema(engine.conn)
    if target_kind not in TARGETS:
        raise ValueError("target_kind must be one of %s" % (TARGETS,))
    if category not in CATEGORIES:
        raise ValueError("category must be one of %s" % (CATEGORIES,))
    if target_kind == "message":
        f = fb.create(engine, target_id, label, should_have, notes, actor=actor)
        engine.conn.execute("UPDATE feedback SET target_kind='message', target_id=?, category=? WHERE id=?", (target_id, category, f["id"]))
        engine.conn.commit()
        return fb.get(engine, f["id"])
    now = engine.now().strftime(ISO)
    if target_kind == "fact_card":
        from . import facts as _facts
        d = _facts.card_detail(engine, target_id)
        if not d:
            raise ValueError("no such fact card")
        conv = row(engine.conn, "SELECT id FROM conversations WHERE patient_id=?", (d["card"]["patient_id"],)) or {"id": 0}
        evidence = {"captured_at": now, "target": "fact_card", "card": d["card"], "source": d["source"], "history": d["history"], "conflicts": d["conflicts"],
                    "software": {"ocp_version": __import__("ocp").__version__, "git_commit": fb.git_commit()},
                    "patient": {"synthetic": bool(row(engine.conn, "SELECT synthetic FROM patients WHERE id=?", (d["card"]["patient_id"],))["synthetic"]), "source_patient_id": d["card"]["source_patient_id"],
                                "display_name": d["card"]["display_name"]}}
        mid = 0
    else:
        from . import referrals as _ref
        d = _ref.detail(engine, target_id)
        if not d:
            raise ValueError("no such referral")
        conv = {"id": d["referral"]["conversation_id"]}
        p = row(engine.conn, "SELECT synthetic, source_patient_id, display_name FROM patients WHERE id=?", (d["referral"]["patient_id"],))
        evidence = {"captured_at": now, "target": "referral", "referral": d["referral"], "events": d["events"], "facts": d["facts"], "thread": d["thread"],
                    "software": {"ocp_version": __import__("ocp").__version__, "git_commit": fb.git_commit()},
                    "patient": {"synthetic": bool(p["synthetic"]), "source_patient_id": p["source_patient_id"], "display_name": p["display_name"]}}
        mid = 0
    cur = engine.conn.execute("INSERT INTO feedback(created_at,actor,conversation_id,message_id,label,should_have,notes,evidence,status,target_kind,target_id,category) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (now, actor, conv["id"], mid, label, should_have, notes, json.dumps(evidence, default=str), "open", target_kind, target_id, category))
    fid = cur.lastrowid
    log_event(engine.conn, now, actor, "feedback_created", conversation_id=conv["id"] or None, detail={"feedback_id": fid, "target_kind": target_kind, "target_id": target_id, "category": category})
    engine.conn.commit()
    return fb.get(engine, fid)


# ------------------------------------------------------------------------------------------------ evaluation cases
def case_from_feedback(engine, fid: int, actor: str = "kate", expect: Optional[Dict] = None) -> Dict:
    """Turn one feedback item into a replayable evaluation case (draft).  For a message flag the setup is the synthetic
    thread up to the flagged reply; the expectation defaults to what Kate wrote (`should_have`) as a free-text check the
    reviewer must sharpen into machine checks (template_in / must_contain / must_not_contain / referral_kind /
    no_referral / order_state / booking_state) before the case becomes `reviewed`."""
    ensure_schema(engine.conn)
    f = fb.get(engine, fid)
    if not f:
        raise ValueError("no such feedback")
    ev = f["evidence"]
    now = engine.now().strftime(ISO)
    target = f.get("target_kind") or "message"
    category = f.get("category") or "operational_effectiveness"
    if target == "message":
        setup = [{"in": t["body"]} for t in ev["thread_through_flagged_message"] if t["direction"] == "inbound"]
        pid = row(engine.conn, "SELECT id FROM patients WHERE source_patient_id=?", (ev["patient"]["source_patient_id"],))
        pre = {}
        if pid:
            from . import facts as _facts
            cards = [c for c in _facts.approved_facts(engine.conn, pid["id"]) if c["class"] == "documented" and c["extraction_method"] != "partner_feed"]
            if cards:
                pre["notes"] = True
                pre["approved_cards"] = [{"kind": c["kind"], "source_ref": c["source_ref"], "excerpt": c["excerpt"],
                                          "source_order_id": (row(engine.conn, "SELECT source_order_id FROM orders WHERE id=?", (c["order_id"],)) or {}).get("source_order_id")} for c in cards]
            evs = rows(engine.conn, "SELECT e.kind, e.at, e.detail, o.source_order_id FROM order_events e JOIN orders o ON o.id=e.order_id WHERE e.patient_id=? AND e.kind IN ('replaced','cancelled','modified','result_finalized') ORDER BY e.id", (pid["id"],))
            ups = []
            for e in evs:
                d = json.loads(e["detail"] or "{}")
                ups.append({k: v for k, v in d.items() if k in ("event_id", "source_order_id", "kind", "at", "lines", "add_lines", "remove_lines", "actor")} or {"source_order_id": e["source_order_id"], "kind": e["kind"], "at": e["at"]})
            if ups:
                pre["updates"] = ups
        case = {"patient": ev["patient"]["source_patient_id"], "policy": {}, "pre": pre, "setup": setup, "expect": expect or {"should_have_text": f["should_have"]},
                "flagged_reply": ev["flagged_message"]["body"], "sim_start": ev.get("sim_clock")}
        title = "feedback #%d: %s" % (fid, f["should_have"][:60])
    elif target == "fact_card":
        c = ev["card"]
        case = {"patient": ev["patient"]["source_patient_id"], "fact_card": {"kind": c["kind"], "source_ref": c["source_ref"], "excerpt": c["excerpt"]},
                "expect": expect or {"card_class": "documented" if f["label"] != "defect" else "unresolved", "should_have_text": f["should_have"]}}
        title = "feedback #%d (fact card %d): %s" % (fid, c["id"], f["should_have"][:50])
    else:
        r = ev["referral"]
        setup = [{"in": t["body"]} for t in ev["thread"] if t["direction"] == "inbound"]
        case = {"patient": ev["patient"]["source_patient_id"], "setup": setup, "expect": expect or {"referral_kind": r["kind"], "receiving_team": r["receiving_team"], "should_have_text": f["should_have"]}}
        title = "feedback #%d (referral %d): %s" % (fid, r["id"], f["should_have"][:50])
    cur = engine.conn.execute("INSERT INTO eval_cases(version,source_feedback_id,category,target_kind,title,case_json,status,created_by,created_at) VALUES(1,?,?,?,?,?,'draft',?,?)",
                              (fid, category, target, title, json.dumps(case, default=str), actor, now))
    cid = cur.lastrowid
    log_event(engine.conn, now, actor, "eval_case_created", detail={"case_id": cid, "feedback_id": fid, "category": category, "target": target})
    engine.conn.commit()
    return get_case(engine, cid)


def get_case(engine, cid: int) -> Optional[Dict]:
    c = row(engine.conn, "SELECT * FROM eval_cases WHERE id=?", (cid,))
    if c:
        c["case"] = json.loads(c["case_json"])
    return c


def review_case(engine, cid: int, actor: str, expect: Optional[Dict] = None, status: str = "reviewed") -> Dict:
    """Sharpen the expectation and move draft → reviewed → active.  A change to an active case creates a new version."""
    ensure_schema(engine.conn)
    c = get_case(engine, cid)
    if not c:
        raise ValueError("no such case")
    if status not in CASE_STATUSES:
        raise ValueError("bad status")
    now = engine.now().strftime(ISO)
    if c["status"] == "active" and expect is not None and expect != c["case"].get("expect"):
        case = dict(c["case"], expect=expect)
        cur = engine.conn.execute("INSERT INTO eval_cases(version,supersedes_id,source_feedback_id,category,target_kind,title,case_json,status,created_by,created_at,reviewed_by,reviewed_at) "
                                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (c["version"] + 1, cid, c["source_feedback_id"], c["category"], c["target_kind"], c["title"], json.dumps(case, default=str), status, actor, now, actor, now))
        engine.conn.execute("UPDATE eval_cases SET status='retired' WHERE id=?", (cid,))
        engine.conn.commit()
        return get_case(engine, cur.lastrowid)
    if expect is not None:
        case = dict(c["case"], expect=expect)
        engine.conn.execute("UPDATE eval_cases SET case_json=? WHERE id=?", (json.dumps(case, default=str), cid))
    engine.conn.execute("UPDATE eval_cases SET status=?, reviewed_by=?, reviewed_at=? WHERE id=?", (status, actor, now, cid))
    log_event(engine.conn, now, actor, "eval_case_reviewed", detail={"case_id": cid, "status": status})
    engine.conn.commit()
    return get_case(engine, cid)


def export_case(engine, cid: int, out_dir: Optional[str] = None) -> str:
    c = get_case(engine, cid)
    if not c:
        raise ValueError("no such case")
    p = row(engine.conn, "SELECT synthetic FROM patients WHERE source_patient_id=?", (c["case"].get("patient"),))
    if p is not None and not p["synthetic"]:
        raise PermissionError("export refused: patient is not marked synthetic")
    d = out_dir or CASES_DIR
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "case-%03d-v%d.json" % (cid, c["version"]))
    with open(path, "w") as f:
        json.dump({"id": cid, "version": c["version"], "category": c["category"], "target_kind": c["target_kind"], "title": c["title"], "status": c["status"],
                   "source_feedback_id": c["source_feedback_id"], "case": c["case"], "exported_at": engine.now().strftime(ISO)}, f, indent=1, default=str)
    engine.conn.execute("UPDATE eval_cases SET exported_path=? WHERE id=?", (os.path.relpath(path, ROOT), cid))
    engine.conn.commit()
    return path


def run_case(case: Dict, policy_overrides: Optional[Dict] = None) -> Dict:
    """Replay a message/referral case on a FRESH synthetic engine (mock adapters, no cost) and check machine expectations.
    Free-text expectations (`should_have_text`) are reported as `needs_review`, never as pass."""
    from tests.helpers import make_engine, conv as _conv, msgs as _msgs, patient as _patient
    from .rules import Policy
    from . import referrals as _ref
    exp = case.get("expect") or {}
    checks: List = []
    if "fact_card" in case:
        return {"status": "needs_review", "checks": [("fact card cases are reviewed by hand against the source", None)]}
    unknown = [k for k in exp if k not in KNOWN_EXPECT_KEYS]
    if unknown:
        return {"status": "error", "checks": [("unknown expectation key(s) %s; nothing was checked" % unknown, False)]}
    pol = Policy(**dict(case.get("policy") or {}, **(policy_overrides or {})))
    eng = make_engine(policy=pol)
    p = row(eng.conn, "SELECT * FROM patients WHERE source_patient_id=?", (case.get("patient"),))
    if not p:
        return {"status": "error", "checks": [("patient not in the synthetic cohort", False)]}
    n = int(case["patient"].split("-")[1])
    # V5-9: reproduce the state the flagged reply depended on — notes, approved cards, provider events — before the texts
    pre = case.get("pre") or {}
    if pre.get("notes"):
        from . import facts as _facts
        import os as _os
        with open(_os.path.join(ROOT, "data", "synthetic_notes.json")) as f:
            _facts.import_notes(eng.conn, json.load(f), eng.now()); eng.conn.commit()
    for c in pre.get("approved_cards") or []:
        from . import facts as _facts
        try:
            cid = _facts.propose(eng, p["id"], c.get("kind", "order_rationale"), "documented", c["excerpt"], c["source_ref"], c["excerpt"], None, None, "manual_review", "replay",
                                 order_id=(row(eng.conn, "SELECT id FROM orders WHERE source_order_id=?", (c.get("source_order_id"),)) or {}).get("id"))
            _facts.review(eng, cid, "replay-reviewer", "clinical_reviewer", "approve", note="replayed approval")
        except ValueError as e:
            checks.append(("replay could not restore card %r: %s" % (c.get("excerpt", "")[:40], e), False))
    for u in pre.get("updates") or []:
        eng.import_updates({"partner_id": p["partner_id"], "generated_at": eng.now().isoformat(), "updates": [u]})
    last = None
    for step in case.get("setup") or []:
        last = eng.handle_inbound(p["phone"], step["in"], None)
    out = [m for m in _msgs(eng, n, "outbound")]
    body = out[-1]["body"] if out else ""
    tpl = out[-1]["template_id"] if out else None
    if "template_in" in exp:
        checks.append(("template in %s" % exp["template_in"], tpl in exp["template_in"]))
    if "not_template" in exp:
        checks.append(("template not %s" % exp["not_template"], tpl != exp["not_template"]))
    for s in exp.get("must_contain") or []:
        checks.append(("contains %r" % s, s.lower() in body.lower()))
    for s in exp.get("must_not_contain") or []:
        checks.append(("does not contain %r" % s, s.lower() not in body.lower()))
    refs = rows(eng.conn, "SELECT kind, receiving_team FROM referrals WHERE conversation_id=?", (_conv(eng, n)["id"],)) if row(eng.conn, "SELECT 1 FROM sqlite_master WHERE name='referrals'") else []
    if "referral_kind" in exp:
        checks.append(("referral kind %s" % exp["referral_kind"], any(r["kind"] == exp["referral_kind"] for r in refs)))
    if "receiving_team" in exp:
        checks.append(("receiving team %s" % exp["receiving_team"], any(r["receiving_team"] == exp["receiving_team"] for r in refs)))
    if exp.get("no_referral"):
        checks.append(("no referral", not refs))
    if "order_state" in exp:
        checks.append(("order state %s" % exp["order_state"], rows(eng.conn, "SELECT state FROM orders WHERE patient_id=?", (p["id"],))[0]["state"] == exp["order_state"]))
    if "conv_state" in exp:
        checks.append(("conversation state %s" % exp["conv_state"], _conv(eng, n)["state"] == exp["conv_state"]))
    if "booking_state" in exp:
        b = rows(eng.conn, "SELECT status FROM bookings WHERE conversation_id=? ORDER BY id DESC LIMIT 1", (_conv(eng, n)["id"],))
        checks.append(("booking state %s" % exp["booking_state"], bool(b) and b[0]["status"] == exp["booking_state"]))
    if "should_have_text" in exp:
        checks.append(("free-text expectation pending a reviewer's machine check: %r" % exp["should_have_text"][:60], None))
    machine = [c for c in checks if c[1] is not None]
    pending = [c for c in checks if c[1] is None]
    if not machine:
        return {"status": "needs_review", "checks": checks, "last_reply": body}
    if any(not c[1] for c in machine):
        return {"status": "fail", "checks": checks, "last_reply": body, "last_template": tpl}
    return {"status": "needs_review" if pending else "pass", "checks": checks, "last_reply": body, "last_template": tpl}


KNOWN_EXPECT_KEYS = {"template_in", "not_template", "must_contain", "must_not_contain", "referral_kind", "receiving_team", "no_referral", "order_state", "conv_state",
                     "booking_state", "should_have_text", "card_class"}


# ------------------------------------------------------------------------------------------------ candidate changes
def propose_change(engine, title: str, description: str, addresses: List[int], case_ids: List[int], actor: str = "kate", change_ref: str = "") -> Dict:
    ensure_schema(engine.conn)
    now = engine.now().strftime(ISO)
    cur = engine.conn.execute("INSERT INTO candidate_changes(title,description,change_ref,addresses,eval_case_ids,status,created_by,created_at) VALUES(?,?,?,?,?,'proposed',?,?)",
                              (title, description, change_ref, json.dumps(addresses), json.dumps(case_ids), actor, now))
    log_event(engine.conn, now, actor, "candidate_change_proposed", detail={"candidate_id": cur.lastrowid, "addresses": addresses, "cases": case_ids})
    engine.conn.commit()
    return get_change(engine, cur.lastrowid)


def get_change(engine, cid: int) -> Optional[Dict]:
    c = row(engine.conn, "SELECT * FROM candidate_changes WHERE id=?", (cid,))
    if c:
        c["addresses"] = json.loads(c["addresses"]); c["eval_case_ids"] = json.loads(c["eval_case_ids"])
        c["eval_results"] = json.loads(c["eval_results"]) if c["eval_results"] else None
    return c


def evaluate_change(engine, cid: int, actor: str = "kate") -> Dict:
    """Run every attached case on a fresh engine with the CURRENT code and record the results on the candidate, together with
    the code revision they were run against.  The candidate is a described change, not an applied artifact: a release
    decision should be recorded only when the candidate's change_ref IS the code the evaluation ran on (the result carries
    `code`; the release tag should match it).  V5-9 makes this explicit rather than implied."""
    ensure_schema(engine.conn)
    c = get_change(engine, cid)
    if not c:
        raise ValueError("no such candidate")
    per = []
    for case_id in c["eval_case_ids"]:
        cs = get_case(engine, case_id)
        if not cs:
            per.append({"case_id": case_id, "status": "missing"}); continue
        r = run_case(cs["case"])
        per.append({"case_id": case_id, "version": cs["version"], "title": cs["title"], "status": r["status"], "checks": r.get("checks"), "last_reply": r.get("last_reply")})
    res = {"run_at": engine.now().strftime(ISO), "passed": sum(1 for p in per if p["status"] == "pass"), "failed": sum(1 for p in per if p["status"] == "fail"),
           "needs_review": sum(1 for p in per if p["status"] == "needs_review"), "per_case": per, "code": fb.git_commit()}
    engine.conn.execute("UPDATE candidate_changes SET eval_results=? WHERE id=?", (json.dumps(res, default=str), cid))
    log_event(engine.conn, res["run_at"], actor, "candidate_change_evaluated", detail={"candidate_id": cid, "passed": res["passed"], "failed": res["failed"]})
    engine.conn.commit()
    return res


def decide_change(engine, cid: int, action: str, actor: str, note: str = "", tag: str = "") -> Dict:
    """approve | release | rollback — recorded decisions with history.  Release requires approval and an evaluation run;
    rollback requires a reason.  Nothing is deployed by this call; it records that a person decided."""
    ensure_schema(engine.conn)
    c = get_change(engine, cid)
    if not c:
        raise ValueError("no such candidate")
    now = engine.now().strftime(ISO)
    if action == "approve":
        if c["status"] != "proposed":
            return {"ok": False, "reason": "only a proposed candidate can be approved"}
        if not c["eval_results"]:
            return {"ok": False, "reason": "evaluate before approving"}
        # V5-9: approval needs at least one reviewed case and every attached case passing, unless an exception is recorded
        r = c["eval_results"]
        if not c["eval_case_ids"] or r.get("passed", 0) == 0 or r.get("failed", 0) or r.get("needs_review", 0):
            if not (note or "").strip().lower().startswith("exception:"):
                return {"ok": False, "reason": "approval needs every attached case passing (%d pass, %d fail, %d needs review); record an explicit 'exception: <why>' to override" % (r.get("passed", 0), r.get("failed", 0), r.get("needs_review", 0))}
        statuses = [x["status"] for x in rows(engine.conn, "SELECT status FROM eval_cases WHERE id IN (%s)" % ",".join("?" * len(c["eval_case_ids"])), c["eval_case_ids"])] if c["eval_case_ids"] else []
        if statuses and any(st not in ("reviewed", "active") for st in statuses) and not (note or "").strip().lower().startswith("exception:"):
            return {"ok": False, "reason": "attached cases must be reviewed or active"}
        engine.conn.execute("UPDATE candidate_changes SET status='approved', approved_by=?, approved_at=? WHERE id=?", (actor, now, cid))
    elif action == "release":
        if c["status"] != "approved":
            return {"ok": False, "reason": "only an approved candidate can be released"}
        if not tag:
            return {"ok": False, "reason": "a release tag (commit or version) is required"}
        engine.conn.execute("UPDATE candidate_changes SET status='released', released_at=?, release_tag=? WHERE id=?", (now, tag, cid))
    elif action == "rollback":
        if c["status"] != "released":
            return {"ok": False, "reason": "only a released candidate can be rolled back"}
        if not note:
            return {"ok": False, "reason": "a rollback reason is required"}
        engine.conn.execute("UPDATE candidate_changes SET status='rolled_back', rolled_back_at=?, rollback_reason=? WHERE id=?", (now, note, cid))
    else:
        raise ValueError("unknown action")
    engine.conn.execute("INSERT INTO release_history(at,actor,candidate_id,action,tag,note) VALUES(?,?,?,?,?,?)", (now, actor, cid, action, tag, note))
    log_event(engine.conn, now, actor, "candidate_change_%s" % action, detail={"candidate_id": cid, "tag": tag, "note": note})
    engine.conn.commit()
    return {"ok": True, "status": get_change(engine, cid)["status"]}


def payload(engine) -> Dict:
    ensure_schema(engine.conn)
    feedback = fb.list_all(engine)
    for f in feedback:
        extra = row(engine.conn, "SELECT target_kind, target_id, category FROM feedback WHERE id=?", (f["id"],)) or {}
        f.update(extra)
    cases = rows(engine.conn, "SELECT id, version, supersedes_id, source_feedback_id, category, target_kind, title, status, created_by, created_at, reviewed_by, exported_path FROM eval_cases ORDER BY id DESC")
    changes = [get_change(engine, c["id"]) for c in rows(engine.conn, "SELECT id FROM candidate_changes ORDER BY id DESC")]
    history = rows(engine.conn, "SELECT * FROM release_history ORDER BY id DESC LIMIT 50")
    return {"feedback": feedback, "eval_cases": cases, "candidate_changes": changes, "release_history": history,
            "categories": CATEGORIES, "targets": TARGETS,
            "separation": {"interaction_logs": "messages/events tables (never used for training)", "evaluation_cases": "eval_cases table + eval/feedback_cases/*.json (reviewed, versioned)",
                           "training_data": "none exists; nothing trains on patient conversations; no behavior change deploys from feedback automatically"},
            "planner": {"mode": "shadow", "note": "the general planner stays in shadow; promotion needs evidence for a narrowly defined action category"}}
