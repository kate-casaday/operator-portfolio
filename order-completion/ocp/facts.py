"""Version 5: fact cards and the chart-review workflow (v5 brief §2; data specification tier 2).

A fact card is a versioned, provenance-backed statement the application may rely on when it talks to a patient.
Extraction is QUOTATION, never interpretation: a rationale card's statement is the note's own sentence, attributed.
Approval authority follows the card's class and kind: the operator (Kate) may propose, edit, flag, and approve
administrative cards; clinical-class cards (order rationale, patient-specific preparation, anything about symptoms,
medication or results) require the designated clinical reviewer.  The role is a labeled simulated switch here.

Classes:  documented   the source says it explicitly (excerpt is a verbatim substring of the source)
          educational  partner-approved general information (not about this patient)
          unresolved   a question, a gap, or conflicting evidence — never told to the patient as fact
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from .db import row, rows, log_event, ensure_tables

ISO = "%Y-%m-%dT%H:%M:%S"

FACT_KINDS = ("order_rationale", "prep_instruction", "care_team", "contact_preference", "general_education", "order_status_note")
CLASSES = ("documented", "educational", "unresolved")
STATUSES = ("proposed", "approved", "flagged", "rejected", "superseded")
METHODS = ("manual_review", "rule_extract", "model_extract", "partner_feed")
ROLES = ("operator", "clinical_reviewer")
# Kinds whose approval requires the designated clinical reviewer.  Preparation text copied verbatim from the partner's
# approved reference (method partner_feed) is administrative; patient-specific preparation from a note is clinical.
CLINICAL_KINDS = {"order_rationale"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS clinical_notes (
  note_id TEXT PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  author TEXT NOT NULL,
  author_role TEXT,
  authored_at TEXT NOT NULL,
  encounter_ref TEXT,
  text TEXT NOT NULL,
  access_basis TEXT NOT NULL,           -- patient_authorized_connection | partner_extract | authorized_manual_review
  imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_cards (
  id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  order_id INTEGER,
  kind TEXT NOT NULL,
  class TEXT NOT NULL,                  -- documented | educational | unresolved
  statement TEXT NOT NULL,              -- what the application may say, attributed; for documented rationale this IS the excerpt
  source_ref TEXT,                      -- note_id / catalog key / feed record
  excerpt TEXT,                         -- verbatim from the source
  author TEXT,
  authored_at TEXT,
  extraction_method TEXT NOT NULL,      -- manual_review | rule_extract | model_extract | partner_feed
  proposed_by TEXT NOT NULL,
  proposed_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'proposed',
  reviewed_by TEXT,
  reviewer_role TEXT,
  verified_at TEXT,
  review_note TEXT,
  requires_clinical_review INTEGER NOT NULL DEFAULT 0,
  conflict_group TEXT,                  -- cards that disagree share a group id
  version INTEGER NOT NULL DEFAULT 1,
  supersedes_id INTEGER,
  superseded_by INTEGER,
  flag_reason TEXT
);
CREATE TABLE IF NOT EXISTS fact_reviews (
  id INTEGER PRIMARY KEY,
  card_id INTEGER NOT NULL REFERENCES fact_cards(id),
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  role TEXT NOT NULL,
  action TEXT NOT NULL,                 -- propose | approve | flag | reject | edit | refuse
  from_status TEXT,
  to_status TEXT,
  note TEXT,
  minutes REAL                          -- chart-review time logged by the reviewer
);
CREATE INDEX IF NOT EXISTS idx_fact_cards_patient ON fact_cards(patient_id);
"""


def ensure_schema(conn) -> None:
    ensure_tables(conn, SCHEMA)          # never executescript mid-transaction (it commits)


# ------------------------------------------------------------------------------------------------ notes
def import_notes(conn, feed: Dict, now) -> Dict:
    """Idempotent on note_id.  Notes arrive only under a record connection or an authorized chart-review agreement;
    `access_basis` is recorded on every note."""
    ensure_schema(conn)
    created = dup = unknown = 0
    for n in feed.get("clinical_notes", []):
        p = row(conn, "SELECT id FROM patients WHERE partner_id=? AND source_patient_id=?", (feed["partner_id"], n["source_patient_id"]))
        if not p:
            unknown += 1
            continue
        if row(conn, "SELECT 1 FROM clinical_notes WHERE note_id=?", (n["note_id"],)):
            dup += 1
            continue
        conn.execute("INSERT INTO clinical_notes(note_id,patient_id,author,author_role,authored_at,encounter_ref,text,access_basis,imported_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (n["note_id"], p["id"], n["author"], n.get("author_role"), n["authored_at"], n.get("encounter_ref"), n["text"],
                      n.get("access_basis", "authorized_manual_review"), now.strftime(ISO)))
        log_event(conn, now.strftime(ISO), "partner_feed", "note_imported", patient_id=p["id"], detail={"note_id": n["note_id"], "access_basis": n.get("access_basis")})
        created += 1
    return {"notes_created": created, "duplicates": dup, "unknown_patients": unknown}


# ------------------------------------------------------------------------------------------------ extraction
_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_RATIONALE_CUES = re.compile(r"\b(to (assess|evaluate|monitor|check|screen|rule out|follow|confirm|look for)|because|due to|for (evaluation|monitoring|screening|follow[- ]?up)|"
                             r"recheck|re-check|workup|work-up|in light of|given|as part of)\b", re.IGNORECASE)


def _test_terms(test_code: str, test_name: str) -> List[str]:
    terms = {test_code.lower(), test_name.lower()}
    alias = {"a1c": ["a1c", "hemoglobin a1c", "hba1c"], "lipid": ["lipid", "lipids", "lipid panel", "cholesterol"], "cbc": ["cbc", "blood count"],
             "cmp": ["cmp", "metabolic panel"], "tsh": ["tsh", "thyroid"], "gtt2": ["glucose tolerance", "gtt"], "ucx": ["urine culture"], "ua": ["urinalysis"],
             "dot5": ["drug screen"], "trop": ["troponin"]}
    terms.update(alias.get(test_code.lower(), []))
    return sorted(terms)


def rule_extract_rationale(engine, patient_id: int, actor: str = "system") -> List[int]:
    """Deterministic extraction over the patient's notes for every open order: a sentence that names the test AND carries
    a rationale cue becomes a proposed DOCUMENTED card whose statement is the sentence verbatim, attributed.  Sentences that
    name the test without a cue become an UNRESOLVED card ("mentioned, no stated reason").  Two documented sentences that
    differ become UNRESOLVED cards sharing a conflict group.  No note naming the test → one UNRESOLVED gap card.
    Returns the ids of cards created.  Nothing here paraphrases."""
    ensure_schema(engine.conn)
    now = engine.now().strftime(ISO)
    created: List[int] = []
    notes = rows(engine.conn, "SELECT * FROM clinical_notes WHERE patient_id=? ORDER BY authored_at", (patient_id,))
    for o in rows(engine.conn, "SELECT * FROM orders WHERE patient_id=? AND state IN ('imported','eligible','outreach_active','escalated','claimed_complete')", (patient_id,)):
        if rows(engine.conn, "SELECT 1 FROM fact_cards WHERE order_id=? AND kind='order_rationale' AND status IN ('proposed','approved','flagged')", (o["id"],)):
            continue                                                    # already has live cards; re-extraction is explicit
        lines = rows(engine.conn, "SELECT test_code, test_name FROM order_lines WHERE order_id=?", (o["id"],))
        terms = sorted({t for l in lines for t in _test_terms(l["test_code"], l["test_name"])}, key=len, reverse=True)
        documented, mentions = [], []
        for n in notes:
            for sent in _SENT_RE.split(n["text"]):
                low = sent.lower()
                if any(t in low for t in terms):
                    (documented if _RATIONALE_CUES.search(sent) else mentions).append((n, sent.strip()))
        if documented:
            distinct = []
            for n, sent in documented:
                if sent not in [d[1] for d in distinct]:
                    distinct.append((n, sent))
            group = "conflict:%d:%s" % (o["id"], now) if len(distinct) > 1 else None
            for n, sent in distinct:
                cid = propose(engine, patient_id, "order_rationale", "unresolved" if group else "documented",
                              statement=sent, source_ref=n["note_id"], excerpt=sent, author=n["author"], authored_at=n["authored_at"],
                              method="rule_extract", actor=actor, order_id=o["id"], conflict_group=group,
                              flag_reason="two notes state different reasons; partner clinical clarification needed" if group else None)
                created.append(cid)
        elif mentions:
            n, sent = mentions[0]
            created.append(propose(engine, patient_id, "order_rationale", "unresolved", statement="The test is mentioned without a stated reason.",
                                   source_ref=n["note_id"], excerpt=sent, author=n["author"], authored_at=n["authored_at"], method="rule_extract",
                                   actor=actor, order_id=o["id"], flag_reason="no documented reason in the notes available; ask the partner"))
        else:
            created.append(propose(engine, patient_id, "order_rationale", "unresolved", statement="No note available to us mentions this order.",
                                   source_ref=None, excerpt=None, author=None, authored_at=None, method="rule_extract", actor=actor, order_id=o["id"],
                                   flag_reason="no documented reason; the reply will say so and point to the clinician's office"))
    engine.conn.commit()
    return created


class ModelExtractor:
    """PREPARED, NOT EXERCISED: a live model proposes candidate sentences; the application accepts only a candidate that is
    a verbatim substring of the note (anything else is rejected before it becomes a card).  Even accepted candidates are
    proposals for the same human review as rule extraction.  Interface kept so the adapter is interchangeable."""
    name = "anthropic"
    simulated = False

    def __init__(self, model: str = "claude-opus-5", client=None):
        self.model, self._client = model, client

    def candidates(self, note_text: str, test_terms: List[str]) -> List[str]:  # pragma: no cover
        raise NotImplementedError("live extraction is not enabled in the prototype")

    @staticmethod
    def accept(note_text: str, candidate: str) -> bool:
        return bool(candidate) and candidate.strip() in note_text


# ------------------------------------------------------------------------------------------------ cards
def propose(engine, patient_id: int, kind: str, klass: str, statement: str, source_ref: Optional[str], excerpt: Optional[str],
            author: Optional[str], authored_at: Optional[str], method: str, actor: str, order_id: Optional[int] = None,
            conflict_group: Optional[str] = None, flag_reason: Optional[str] = None, supersedes: Optional[int] = None) -> int:
    ensure_schema(engine.conn)
    assert kind in FACT_KINDS and klass in CLASSES and method in METHODS
    if klass == "documented" and method in ("rule_extract", "manual_review", "model_extract"):
        # V5-2: a documented card must quote an EXISTING note that belongs to this patient; author/date come from the note
        src = row(engine.conn, "SELECT * FROM clinical_notes WHERE note_id=?", (source_ref,)) if source_ref else None
        if not src:
            raise ValueError("a documented card must reference an existing clinical note")
        if src["patient_id"] != patient_id:
            raise ValueError("the referenced note belongs to a different patient")
        if not excerpt or excerpt.strip() not in src["text"]:
            raise ValueError("a documented card's excerpt must be verbatim from its source")
        author, authored_at = src["author"], src["authored_at"]          # never trust caller-supplied attribution
    now = engine.now().strftime(ISO)
    requires = 1 if (kind in CLINICAL_KINDS or (kind == "prep_instruction" and method != "partner_feed") or has_clinical_content(statement)) else 0
    version = 1
    if supersedes:
        prev = row(engine.conn, "SELECT version FROM fact_cards WHERE id=?", (supersedes,))
        version = (prev["version"] if prev else 0) + 1
    status = "flagged" if klass == "unresolved" else "proposed"
    cur = engine.conn.execute("INSERT INTO fact_cards(patient_id,order_id,kind,class,statement,source_ref,excerpt,author,authored_at,extraction_method,proposed_by,proposed_at,"
                              "status,requires_clinical_review,conflict_group,version,supersedes_id,flag_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (patient_id, order_id, kind, klass, statement, source_ref, excerpt, author, authored_at, method, actor, now, status, requires,
                               conflict_group, version, supersedes, flag_reason))
    cid = cur.lastrowid
    if supersedes:
        engine.conn.execute("UPDATE fact_cards SET status='superseded', superseded_by=? WHERE id=?", (cid, supersedes))
    engine.conn.execute("INSERT INTO fact_reviews(card_id,at,actor,role,action,from_status,to_status,note) VALUES(?,?,?,?,?,?,?,?)",
                        (cid, now, actor, "system" if actor == "system" else "operator", "propose", None, status, flag_reason))
    log_event(engine.conn, now, actor, "fact_card_proposed", patient_id=patient_id, order_id=order_id,
              detail={"card_id": cid, "kind": kind, "class": klass, "method": method, "requires_clinical_review": bool(requires), "conflict_group": conflict_group})
    return cid


_CLINICAL_CONTENT_RE = re.compile(r"\b(stop|start|skip|hold|take|taking|dose|dosage|medication|medications|meds|pill|pills|statin|insulin|warfarin|metformin|"
                                  r"result|results|normal|abnormal|diagnos\w*|symptom\w*|safe|dangerous|risk|proves?|treat\w*|disease|infection|cancer|pregnan\w*|"
                                  r"fasting|fast|eat|drink|because)\b", re.IGNORECASE)


def has_clinical_content(text: str) -> bool:
    """Any card whose statement carries medication / results / diagnosis / instruction wording needs the clinical reviewer,
    whatever its kind (V5-2: an `order_status_note` saying 'stop your statin' is clinical content)."""
    return bool(_CLINICAL_CONTENT_RE.search(text or ""))


def verify_card_source(conn, card: Dict) -> Optional[str]:
    """Re-check a documented card against its source at approval AND at use: the note must exist, belong to the patient,
    and still contain the excerpt.  Returns a reason when it fails."""
    if card["class"] != "documented":
        return None
    if card["extraction_method"] == "partner_feed":
        return None
    src = row(conn, "SELECT patient_id, text FROM clinical_notes WHERE note_id=?", (card["source_ref"],)) if card["source_ref"] else None
    if not src:
        return "source note missing"
    if src["patient_id"] != card["patient_id"]:
        return "source note belongs to another patient"
    if not card["excerpt"] or card["excerpt"].strip() not in src["text"]:
        return "excerpt is not verbatim in the source"
    if (card["statement"] or "").strip() != card["excerpt"].strip():
        return "statement differs from the excerpt"
    return None


def can_approve(card: Dict, role: str) -> bool:
    if role == "clinical_reviewer":
        return True
    return role == "operator" and not card["requires_clinical_review"]


def review(engine, card_id: int, actor: str, role: str, action: str, statement: Optional[str] = None, note: str = "",
           minutes: Optional[float] = None) -> Dict:
    """approve | flag | reject | edit.  An edit supersedes the card with a new version (same provenance) that goes back to
    `proposed`.  An unresolved card cannot be approved by anyone: it must be edited into a documented card with a verbatim
    excerpt (or stay flagged).  Refusals are recorded, not silent."""
    ensure_schema(engine.conn)
    if role not in ROLES:
        raise ValueError("role must be one of %s" % (ROLES,))
    card = row(engine.conn, "SELECT * FROM fact_cards WHERE id=?", (card_id,))
    if not card:
        raise ValueError("no such card")
    now = engine.now().strftime(ISO)

    def audit(act, frm, to, n):
        engine.conn.execute("INSERT INTO fact_reviews(card_id,at,actor,role,action,from_status,to_status,note,minutes) VALUES(?,?,?,?,?,?,?,?,?)",
                            (card_id, now, actor, role, act, frm, to, n, minutes))
        if minutes:
            engine.conn.execute("INSERT INTO human_time(at,actor,activity,minutes,source) VALUES(?,?,?,?,'logged')", (now, actor, "chart_review:card_%d" % card_id, minutes))
        log_event(engine.conn, now, actor, "fact_card_review", patient_id=card["patient_id"], order_id=card["order_id"],
                  detail={"card_id": card_id, "action": act, "role": role, "from": frm, "to": to, "note": n[:200] if n else None})

    if action == "approve":
        if card["status"] in ("superseded", "rejected"):
            raise ValueError("card is %s" % card["status"])
        if card["class"] == "unresolved":
            audit("refuse", card["status"], card["status"], "unresolved cards cannot be approved; edit into a documented card with a verbatim excerpt or leave flagged")
            engine.conn.commit()
            return {"ok": False, "reason": "unresolved cards cannot be approved"}
        if not can_approve(card, role):
            audit("refuse", card["status"], card["status"], "requires the designated clinical reviewer; operator role cannot approve")
            engine.conn.commit()
            return {"ok": False, "reason": "requires clinical reviewer"}
        bad = verify_card_source(engine.conn, card)
        if bad:
            audit("refuse", card["status"], card["status"], "source check failed at approval: %s" % bad)
            engine.conn.commit()
            return {"ok": False, "reason": "source check failed: %s" % bad}
        siblings = rows(engine.conn, "SELECT id FROM fact_cards WHERE conflict_group=? AND id!=? AND status IN ('proposed','flagged')", (card["conflict_group"], card_id)) if card["conflict_group"] else []
        if siblings:
            # V5-2: approving one side of a conflict is a clinical decision that must name what happens to the other side
            if role != "clinical_reviewer" or not (note or "").strip():
                audit("refuse", card["status"], card["status"], "conflicting cards: the clinical reviewer must approve with a note that resolves the conflict")
                engine.conn.commit()
                return {"ok": False, "reason": "conflict needs the clinical reviewer and a resolution note"}
            for sib in siblings:
                engine.conn.execute("UPDATE fact_cards SET status='rejected', reviewed_by=?, reviewer_role=?, review_note=? WHERE id=?",
                                    (actor, role, "conflict resolved by approval of card #%d: %s" % (card_id, note), sib["id"]))
                engine.conn.execute("INSERT INTO fact_reviews(card_id,at,actor,role,action,from_status,to_status,note) VALUES(?,?,?,?,?,?,?,?)",
                                    (sib["id"], now, actor, role, "reject", "flagged", "rejected", "conflict resolved by approval of #%d: %s" % (card_id, note)))
        engine.conn.execute("UPDATE fact_cards SET status='approved', reviewed_by=?, reviewer_role=?, verified_at=?, review_note=? WHERE id=?", (actor, role, now, note, card_id))
        audit("approve", card["status"], "approved", note)
    elif action == "flag":
        engine.conn.execute("UPDATE fact_cards SET status='flagged', flag_reason=?, reviewed_by=?, reviewer_role=? WHERE id=?", (note or "flagged for partner clinical clarification", actor, role, card_id))
        audit("flag", card["status"], "flagged", note)
    elif action == "reject":
        engine.conn.execute("UPDATE fact_cards SET status='rejected', reviewed_by=?, reviewer_role=?, review_note=? WHERE id=?", (actor, role, note, card_id))
        audit("reject", card["status"], "rejected", note)
    elif action == "edit":
        if not statement:
            raise ValueError("edit needs a statement")
        klass = card["class"]
        excerpt = card["excerpt"]
        if card["kind"] in CLINICAL_KINDS:
            # a clinical statement may only be the source's own words: the edited statement must be verbatim in the source
            src = row(engine.conn, "SELECT text FROM clinical_notes WHERE note_id=?", (card["source_ref"],)) if card["source_ref"] else None
            if src and statement.strip() in src["text"]:
                klass, excerpt = "documented", statement.strip()
            else:
                audit("refuse", card["status"], card["status"], "a rationale statement must be a verbatim sentence from the source note; paraphrase is not allowed")
                engine.conn.commit()
                return {"ok": False, "reason": "statement is not verbatim from the source"}
        new_id = propose(engine, card["patient_id"], card["kind"], klass, statement.strip(), card["source_ref"], excerpt, card["author"], card["authored_at"],
                         "manual_review", actor, order_id=card["order_id"], conflict_group=card["conflict_group"], supersedes=card_id)   # a conflict follows the card
        audit("edit", card["status"], "superseded", note)
        engine.conn.commit()
        return {"ok": True, "card_id": new_id, "superseded": card_id}
    else:
        raise ValueError("unknown action")
    engine.conn.commit()
    return {"ok": True, "card_id": card_id}


def approved_facts(conn, patient_id: int, kind: Optional[str] = None, order_id: Optional[int] = None) -> List[Dict]:
    ensure_schema(conn)
    q = "SELECT * FROM fact_cards WHERE patient_id=? AND status='approved'"
    args: List = [patient_id]
    if kind:
        q += " AND kind=?"; args.append(kind)
    if order_id:
        q += " AND order_id=?"; args.append(order_id)
    return rows(conn, q + " ORDER BY id", args)


def open_questions(conn, patient_id: int) -> List[Dict]:
    ensure_schema(conn)
    return rows(conn, "SELECT * FROM fact_cards WHERE patient_id=? AND status='flagged' ORDER BY id", (patient_id,))


def rationale_for_reply(engine, patient_id: int) -> Dict:
    """What the application may say about WHY the open orders exist: approved documented cards (verbatim, attributed) or an
    honest gap.  Never an inference."""
    ensure_schema(engine.conn)
    open_ids = [o["id"] for o in rows(engine.conn, "SELECT id FROM orders WHERE patient_id=? AND state IN ('eligible','outreach_active','escalated','claimed_complete')", (patient_id,))]
    cards = [c for c in approved_facts(engine.conn, patient_id, "order_rationale") if c["class"] == "documented" and (not open_ids or c["order_id"] in open_ids)
             and verify_card_source(engine.conn, c) is None]                      # V5-2: re-verified at the moment of use
    gaps = [c for c in open_questions(engine.conn, patient_id) if c["kind"] == "order_rationale"]
    return {"documented": [{"card_id": c["id"], "excerpt": c["excerpt"], "author": c["author"], "authored_at": c["authored_at"], "order_id": c["order_id"]} for c in cards],
            "unresolved": [{"card_id": c["id"], "reason": c["flag_reason"], "conflict_group": c["conflict_group"]} for c in gaps]}


# ------------------------------------------------------------------------------------------------ payloads
def list_cards(engine, status: Optional[str] = None, kind: Optional[str] = None, patient_id: Optional[int] = None) -> List[Dict]:
    ensure_schema(engine.conn)
    q = ("SELECT f.*, p.display_name, p.source_patient_id, o.source_order_id, (SELECT GROUP_CONCAT(l.test_name) FROM order_lines l WHERE l.order_id=f.order_id) tests "
         "FROM fact_cards f JOIN patients p ON p.id=f.patient_id LEFT JOIN orders o ON o.id=f.order_id WHERE 1=1")
    args: List = []
    if status:
        q += " AND f.status=?"; args.append(status)
    if kind:
        q += " AND f.kind=?"; args.append(kind)
    if patient_id:
        q += " AND f.patient_id=?"; args.append(patient_id)
    return rows(engine.conn, q + " ORDER BY f.id DESC", args)


def card_detail(engine, card_id: int) -> Optional[Dict]:
    ensure_schema(engine.conn)
    c = row(engine.conn, "SELECT f.*, p.display_name, p.source_patient_id, o.source_order_id FROM fact_cards f JOIN patients p ON p.id=f.patient_id "
                         "LEFT JOIN orders o ON o.id=f.order_id WHERE f.id=?", (card_id,))
    if not c:
        return None
    src = row(engine.conn, "SELECT * FROM clinical_notes WHERE note_id=?", (c["source_ref"],)) if c["source_ref"] else None
    history = rows(engine.conn, "SELECT * FROM fact_reviews WHERE card_id=? ORDER BY id", (card_id,))
    chain = []
    cur = c
    while cur and cur.get("supersedes_id"):
        cur = row(engine.conn, "SELECT id, version, statement, status, proposed_by, proposed_at FROM fact_cards WHERE id=?", (cur["supersedes_id"],))
        if cur:
            chain.append(cur)
    conflicts = rows(engine.conn, "SELECT id, statement, source_ref, author, status FROM fact_cards WHERE conflict_group=? AND id!=?", (c["conflict_group"], card_id)) if c["conflict_group"] else []
    return {"card": c, "source": src, "history": history, "supersedes_chain": chain, "conflicts": conflicts,
            "approval": {"requires_clinical_review": bool(c["requires_clinical_review"]), "operator_may_approve": can_approve(c, "operator") and c["class"] != "unresolved",
                         "note": "clinical-class cards need the designated clinical reviewer; unresolved cards cannot be approved, only edited into a verbatim documented card or flagged"}}
