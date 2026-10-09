"""Feed integrity (Sept 23, 2026; reconciled twice the same day after Codex's reviews): the automated partner data feed
and the checks and balances that let StealthCo catch a problem itself, instead of waiting for the health system's
technology team to notice one.

Operating principle (Kate, Sept 23, 2026): never outsource to the health system's technology team work we can do
ourselves.  Those teams are understaffed and overworked; anything that becomes a queue on their side sets our pace.

What this module does
  * arrival paths: a pickup from an inbox folder (a stand-in for an SFTP/HTTPS pickup; it runs when the scheduler tick
    runs, nothing here schedules itself), a push endpoint and the updates API.  All call `receive()`; nothing reaches the
    importer unvalidated; a bad file is a recorded outcome (a receipt), never an exception.
  * validation on arrival, both streams of a payload: readable → types → envelope → right partner → shape → sequence
    per stream (replay / conflict / late arrival / stalled) → schema-required fields → row checks (orders and events,
    kind-specific payloads, enums, timestamps canonicalised to the naive-local contract) → duplicates by the importer's
    own identity → identifier re-keying (distinct, normalised patients) → encoding → volume against the partner's own
    history → result coverage → test codes.
  * verdicts: accepted / accepted_with_warnings (bad rows quarantined; an existing patient touched by a quarantined row
    is held from outreach until a clean row arrives; holds are written inside the import transaction) / accepted_replay
    (same bytes as a file already accepted: a NO-OP on state — detected before validation, nothing imported, no health,
    baseline, alert or hold change) / rejected (held; nothing applied — one transaction; every non-safety message for
    that partner stops at the send boundary until a newer good file arrives or an operator clears the block with a note).
  * alerts: one open alert per incident; the exact message the partner's technical contact receives is stored, with
    what we did and what we need; identifiers only, never names or numbers.  Recovery closes late / silent incidents so
    the next outage is a new message.  Notification through an adapter (simulated here).
  * monitoring on the tick: a file that has not arrived (no upper cutoff; also a stream that never started) and a
    results stream that has gone silent while orders keep arriving.
  * evidence: the original payload of every non-local file is retained on the receipt; a named operator can REPROCESS a
    receipt, which applies only its events stream (deferred events for orders that now exist) and never its orders stream,
    so no older demographics are ever written over newer partner corrections (acceptance watermark and baseline untouched; the
    results clock may advance).  The receipt of an applied import is written inside the import transaction: `rejected` can only
    mean nothing was committed; a failure after the commit becomes an operator item against the committed receipt.

Design record: strategy/feed-integrity-brief-2026-09-23.md.  Everything here is synthetic; no adapter contacts anyone.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .db import log_event, row, rows, set_setting, ensure_tables
from .importer import FeedRejected, normalize_feed, _event_id

ISO = "%Y-%m-%dT%H:%M:%S"
PAUSE_REASON = "policy:feed_integrity"

SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_receipts (
  id INTEGER PRIMARY KEY,
  partner_id TEXT,
  feed_kind TEXT,                        -- orders | updates | orders+updates | unknown
  source TEXT NOT NULL,                  -- pull | push | api | internal (seed / test / scenario) | reprocess
  source_name TEXT,
  received_at TEXT NOT NULL,
  generated_at TEXT,
  byte_size INTEGER,
  sha1 TEXT,
  record_count INTEGER,
  quarantined_count INTEGER NOT NULL DEFAULT 0,
  verdict TEXT NOT NULL,                 -- accepted | accepted_with_warnings | accepted_replay | rejected
  applied INTEGER NOT NULL DEFAULT 0,    -- 1 only when the import transaction committed
  checks TEXT NOT NULL,                  -- JSON [{check, status, detail, rows}]
  import_result TEXT,                    -- JSON from the importer when imported
  quarantined TEXT                       -- JSON rows held back (source ids, field names, reasons; no values)
);
CREATE TABLE IF NOT EXISTS feed_payloads (
  receipt_id INTEGER PRIMARY KEY,
  payload BLOB NOT NULL,                 -- the bytes as received (push / pull / api); a rejected file can be inspected and reprocessed
  retained_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_health (
  partner_id TEXT NOT NULL,
  feed_kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok',     -- ok | warning | blocked
  blocked_reason TEXT,
  blocked_since TEXT,
  first_accepted_at TEXT,
  last_accepted_at TEXT,
  last_generated_at TEXT,
  last_sha1 TEXT,
  last_record_count INTEGER,
  accepted_files INTEGER NOT NULL DEFAULT 0,
  rejected_files INTEGER NOT NULL DEFAULT 0,
  consecutive_rejections INTEGER NOT NULL DEFAULT 0,
  baseline_count REAL,                   -- exponential moving average of accepted, non-replay record counts
  baseline_n INTEGER NOT NULL DEFAULT 0,
  last_result_event_at TEXT,             -- updates kind: newest result/cancellation event actually APPLIED
  updated_at TEXT NOT NULL,
  PRIMARY KEY (partner_id, feed_kind)
);
CREATE TABLE IF NOT EXISTS feed_holds (
  partner_id TEXT NOT NULL,
  source_patient_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  receipt_id INTEGER,
  since TEXT NOT NULL,
  PRIMARY KEY (partner_id, source_patient_id)
);
CREATE TABLE IF NOT EXISTS feed_alerts (
  id INTEGER PRIMARY KEY,
  partner_id TEXT NOT NULL,
  feed_kind TEXT,
  receipt_id INTEGER,
  code TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,              -- one open alert per key (an incident, not an observation)
  severity TEXT NOT NULL,                -- blocking | warning | info
  partner_action_needed INTEGER NOT NULL DEFAULT 0,
  subject TEXT NOT NULL,
  message_to_partner TEXT NOT NULL,      -- the exact text the partner's technical contact receives
  message_internal TEXT NOT NULL,        -- what the operator sees
  status TEXT NOT NULL DEFAULT 'open',   -- open | acknowledged | resolved
  occurrences INTEGER NOT NULL DEFAULT 1,
  opened_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  notified_at TEXT,
  notify_channel TEXT,
  notify_ref TEXT,
  notify_error TEXT,
  acknowledged_at TEXT,
  acknowledged_by TEXT,
  acknowledged_note TEXT,
  resolved_at TEXT,
  resolved_by TEXT,
  resolution TEXT
);
"""


def ensure_schema(conn) -> None:
    ensure_tables(conn, SCHEMA)


# ----------------------------------------------------------------------------------------------- notification adapter
@dataclass
class NotifyResult:
    channel: str
    ref: str
    simulated: bool


class TechContactAdapter:
    """How the partner's technical contact is told.  Production: email or a ticket in the partner's system, addressed to
    the contact on the partner directory.  The interface is the same; only the transport changes."""
    name = "base"
    simulated = True

    def notify(self, partner_id: str, contact: Dict, subject: str, body: str) -> NotifyResult:  # pragma: no cover
        raise NotImplementedError


class SimulatedTechContact(TechContactAdapter):
    name = "simulated"
    simulated = True

    def __init__(self, fail_times: int = 0):
        self.sent: List[Dict] = []
        self.fail_times = fail_times

    def notify(self, partner_id: str, contact: Dict, subject: str, body: str) -> NotifyResult:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("simulated notification failure")
        ref = "NOTIFY-" + uuid.uuid4().hex[:10]
        self.sent.append({"partner_id": partner_id, "to": contact.get("email") or contact.get("name"), "subject": subject, "body": body, "ref": ref})
        return NotifyResult(channel="simulated", ref=ref, simulated=True)


# ----------------------------------------------------------------------------------------------- pull adapter
class FeedSource:
    """A pickup.  `pending()` lists files waiting; `claim()` takes one so no other worker reads it; `read()` returns
    bytes; `done()` files the result.  Nothing here runs by itself: the scheduler tick drives it."""
    name = "base"
    simulated = True

    def pending(self) -> List[str]:  # pragma: no cover
        return []

    def claim(self, name: str) -> str:  # pragma: no cover
        return name

    def read(self, name: str) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def done(self, name: str, verdict: str) -> None:  # pragma: no cover
        pass


class InboxFeedSource(FeedSource):
    """Files in a folder are picked up when the scheduler tick runs.  A file is CLAIMED (renamed) before it is read, so a
    second worker cannot take it; after the import commits it is moved to processed/ or quarantine/ by verdict.  A crash
    between commit and move leaves a claimed file behind; it is offered again after `reclaim_after_s`, and because the
    receipt records the file's hash, the second pass is an idempotent replay, not a second application.  Delivery is
    therefore AT-LEAST-ONCE WITH IDEMPOTENT REPLAY, not exactly-once.  Files modified within `settle_s` are left alone
    (still being written).  Stand-in for an SFTP/HTTPS pickup job; `path=None` disables it."""
    name = "inbox"
    simulated = True

    def __init__(self, path: Optional[str], settle_s: float = 0.0, reclaim_after_s: float = 600.0):
        self.path = path
        self.settle_s = settle_s
        self.reclaim_after_s = reclaim_after_s

    def pending(self) -> List[str]:
        if not self.path or not os.path.isdir(self.path):
            return []
        out = []
        now = time.time()
        for n in os.listdir(self.path):
            p = os.path.join(self.path, n)
            if not os.path.isfile(p) or n.startswith("."):
                continue
            age = now - os.path.getmtime(p)
            if ".claimed-" in n:
                if age >= self.reclaim_after_s:
                    out.append(n)                        # abandoned claim: offer it again (idempotent replay)
                continue
            if age >= self.settle_s:
                out.append(n)
        return sorted(out, key=lambda n: (os.path.getmtime(os.path.join(self.path, n)), n))

    def claim(self, name: str) -> str:
        if ".claimed-" in name:
            return name
        claimed = "%s.claimed-%s" % (name, uuid.uuid4().hex[:8])
        os.rename(os.path.join(self.path, name), os.path.join(self.path, claimed))   # atomic on one filesystem
        return claimed

    def read(self, name: str) -> bytes:
        with open(os.path.join(self.path, name), "rb") as f:
            return f.read()

    def done(self, name: str, verdict: str) -> None:
        sub = "processed" if verdict != "rejected" else "quarantine"
        os.makedirs(os.path.join(self.path, sub), exist_ok=True)
        original = name.split(".claimed-")[0]
        shutil.move(os.path.join(self.path, name), os.path.join(self.path, sub, original))


# ----------------------------------------------------------------------------------------------- timestamps
def parse_ts(v) -> Optional[datetime]:
    """The one timestamp contract: ISO 8601; an offset (`Z` or ±hh:mm) is accepted and DROPPED, i.e. the value is read
    as the partner's wall-clock time (the prototype's clock is naive local).  Returns None when unparseable."""
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.replace(tzinfo=None)
    return d.replace(microsecond=0)


def canon_ts(v) -> Optional[str]:
    d = parse_ts(v)
    return d.strftime(ISO) if d else None


def _has_offset(v) -> bool:
    return isinstance(v, str) and bool(re.search(r"(Z|z|[+-]\d{2}:?\d{2})$", v.strip()))


# ----------------------------------------------------------------------------------------------- checks
@dataclass
class Check:
    check: str
    status: str            # pass | warn | fail
    detail: str = ""
    rows: List[Dict] = field(default_factory=list)

    def d(self) -> Dict:
        return {"check": self.check, "status": self.status, "detail": self.detail, "rows": self.rows[:20]}


_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_MOJIBAKE = re.compile("[Ãâ�]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
EVENT_KINDS = {"result_finalized", "cancelled", "replaced", "modified", "attended"}
ORDER_STATES = {"open", "cancelled", "resulted", "replaced"}
LINE_STATUSES = {"outstanding", "resulted", "cancelled"}
_TS_ORDER_FIELDS = ("ordered_at", "intended_due_at", "visit_at", "resulted_at")


def _schema_required() -> Dict[str, List[str]]:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        s = json.load(open(os.path.join(here, "data", "schema", "partner-data-v5.schema.json")))
    except Exception:  # noqa: BLE001
        return {}
    return {"envelope": list(s.get("required") or [])}


def _norm_name(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().casefold()


def _norm_phone(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def decode(payload) -> Tuple[Optional[Dict], Optional[bytes], Optional[Check]]:
    """bytes/str/dict → (feed dict, raw bytes, failing check)."""
    raw = None
    if isinstance(payload, dict):
        return payload, None, None
    if isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            return None, raw, Check("readable", "fail", "file is not valid UTF-8 (%s)" % e.reason)
    elif isinstance(payload, str):
        text = payload
        raw = text.encode("utf-8")
    else:
        return None, None, Check("readable", "fail", "payload is a %s, not a file or an object" % type(payload).__name__)
    try:
        feed = json.loads(text)
    except json.JSONDecodeError as e:
        return None, raw, Check("readable", "fail", "file is not valid JSON at line %d column %d" % (e.lineno, e.colno))
    if not isinstance(feed, dict):
        return None, raw, Check("readable", "fail", "top level is a %s, not an object" % type(feed).__name__)
    return feed, raw, None


def _lines_ok(lines) -> Optional[str]:
    if not isinstance(lines, list) or not lines:
        return "no order lines"
    for l in lines:
        if not isinstance(l, dict) or not isinstance(l.get("test_code"), str) or not l.get("test_code") or not isinstance(l.get("test_name"), str) or not l.get("test_name"):
            return "a line lacks test_code/test_name"
        if "status" in l and l.get("status") not in LINE_STATUSES:
            return "line status is not one of %s" % sorted(LINE_STATUSES)
    return None


def _check_order_row(rec: Dict, now: datetime, catalog) -> Tuple[List[str], Dict]:
    """→ (problems, stats).  Reasons name fields, never values (they travel to the partner)."""
    problems: List[str] = []
    st = {"consent_absent": 0, "encoding": 0, "old": 0, "codes": set(), "state_absent": 0, "offset": False}
    p = rec.get("patient") if isinstance(rec.get("patient"), dict) else None
    if not isinstance(rec.get("source_order_id"), str) or not rec.get("source_order_id"):
        problems.append("missing source_order_id")
    if p is None:
        problems.append("missing patient block")
    else:
        if not isinstance(p.get("source_patient_id"), str) or not p.get("source_patient_id"):
            problems.append("missing patient.source_patient_id")
        if not isinstance(p.get("display_name"), str) or not p.get("display_name"):
            problems.append("missing patient.display_name")
        ph = p.get("phone")
        if ph is not None and (not isinstance(ph, str) or not _E164.match(ph)):
            problems.append("patient.phone is not E.164 (+ and 8-15 digits)")
        if not isinstance(p.get("consent_sms"), bool):
            st["consent_absent"] = 1
            problems.append("patient.consent_sms missing or not true/false")
        for v in (p.get("display_name"), p.get("home_town")):
            if isinstance(v, str) and (_MOJIBAKE.search(v) or _CONTROL.search(v)):
                st["encoding"] += 1
    oa = parse_ts(rec.get("ordered_at"))
    if oa is None:
        problems.append("ordered_at missing or not ISO 8601")
    elif oa > now + timedelta(hours=1):
        problems.append("ordered_at is in the future")
    elif (now - oa).days > 730:
        st["old"] = 1
    for f in _TS_ORDER_FIELDS:
        v = rec.get(f)
        if v is not None and f != "ordered_at" and parse_ts(v) is None:
            problems.append("%s is not ISO 8601" % f)
        if _has_offset(v):
            st["offset"] = True
    if "state" not in rec:
        st["state_absent"] = 1
    elif rec.get("state") not in ORDER_STATES:
        problems.append("state is not one of %s" % sorted(ORDER_STATES))
    e = _lines_ok(rec.get("lines"))
    if e:
        problems.append(e)
    elif catalog is not None:
        try:
            st["codes"] = set(catalog.unknown_codes([l["test_code"] for l in rec["lines"]]))
        except Exception:  # noqa: BLE001
            pass
    return problems, st


def _check_event_row(u: Dict, now: datetime) -> Tuple[List[str], Dict]:
    problems: List[str] = []
    st = {"result": 0, "offset": _has_offset(u.get("at"))}
    if not isinstance(u.get("source_order_id"), str) or not u.get("source_order_id"):
        problems.append("missing source_order_id")
    k = u.get("kind")
    if not isinstance(k, str) or k not in EVENT_KINDS:
        problems.append("kind is not one of %s" % sorted(EVENT_KINDS))
    at = parse_ts(u.get("at"))
    if at is None:
        problems.append("at missing or not ISO 8601")
    elif at > now + timedelta(hours=1):
        problems.append("at is in the future")
    if k == "result_finalized":
        ls = u.get("lines")
        if not isinstance(ls, list) or not ls or not all(isinstance(c, str) and c for c in ls):
            problems.append("result_finalized needs a non-empty list of test codes in `lines`")
        st["result"] = 1
    elif k == "cancelled":
        st["result"] = 1
    elif k == "replaced":
        rep = u.get("replacement")
        if not isinstance(rep, dict) or not isinstance(rep.get("source_order_id"), str) or not rep.get("source_order_id"):
            problems.append("replaced needs a `replacement` order with source_order_id and lines")
        else:
            e = _lines_ok(rep.get("lines"))
            if e:
                problems.append("replacement: %s" % e)
            for f in _TS_ORDER_FIELDS:
                if rep.get(f) is not None and parse_ts(rep.get(f)) is None:
                    problems.append("replacement.%s is not ISO 8601" % f)
    elif k == "modified":
        adds, rem = u.get("add_lines"), u.get("remove_lines")
        if adds is None and rem is None:
            problems.append("modified needs add_lines and/or remove_lines")
        if adds is not None and _lines_ok(adds):
            problems.append("modified.add_lines: %s" % _lines_ok(adds))
        if rem is not None and (not isinstance(rem, list) or not all(isinstance(c, str) for c in rem)):
            problems.append("modified.remove_lines must be a list of test codes")
    return problems, st


def _canon_order(rec: Dict) -> Dict:
    out = dict(rec)
    for f in _TS_ORDER_FIELDS:
        if out.get(f) is not None:
            out[f] = canon_ts(out[f]) or out[f]
    return out


def _canon_event(u: Dict) -> Dict:
    out = dict(u)
    if out.get("at") is not None:
        out["at"] = canon_ts(out["at"]) or out["at"]
    if isinstance(out.get("replacement"), dict):
        out["replacement"] = _canon_order(out["replacement"])
    return out


def validate(feed: Dict, expected_partner: str, now: datetime, conn, policy, catalog=None, watermarks: Optional[Dict[str, Dict]] = None,
             baselines: Optional[Dict[str, Tuple[float, int]]] = None) -> Tuple[List[Check], Dict, List[Dict], List[str]]:
    """Returns (checks, cleaned feed for the importer with bad rows removed and timestamps canonicalised, quarantined rows,
    stream kinds present).  Both streams of a payload are validated; the caller applies both or neither."""
    checks: List[Check] = []
    quarantined: List[Dict] = []
    watermarks = watermarks or {}
    baselines = baselines or {}

    # 1. types and envelope
    pid, gen = feed.get("partner_id"), feed.get("generated_at")
    if not isinstance(pid, str) or not pid or not isinstance(gen, str) or not gen:
        checks.append(Check("envelope", "fail", "partner_id and generated_at must be non-empty strings"))
        return checks, feed, quarantined, []
    g = parse_ts(gen)
    if g is None:
        checks.append(Check("envelope", "fail", "generated_at is not an ISO 8601 timestamp"))
        return checks, feed, quarantined, []
    if g > now + timedelta(hours=1):
        checks.append(Check("envelope", "fail", "generated_at %s is in the future (our clock %s)" % (g.strftime(ISO), now.strftime(ISO))))
        return checks, feed, quarantined, []
    checks.append(Check("envelope", "pass", "partner_id and generated_at present"))
    for k in ("orders", "updates", "order_events", "patients"):
        if k in feed and not isinstance(feed[k], list):
            checks.append(Check("shape", "fail", "%s must be a list" % k))
            return checks, feed, quarantined, []
    if "patients" in feed and not all(isinstance(p, dict) for p in feed["patients"]):
        checks.append(Check("shape", "fail", "patients must be objects"))
        return checks, feed, quarantined, []

    # 2. right partner
    if pid != expected_partner:
        checks.append(Check("partner", "fail", "file names another partner; this deployment serves %r" % expected_partner))
        return checks, feed, quarantined, []
    checks.append(Check("partner", "pass", pid))

    # 3. shape (the importer's own normalisation)
    try:
        norm = normalize_feed(feed)
    except FeedRejected as e:
        checks.append(Check("shape", "fail", str(e)))
        return checks, feed, quarantined, []
    except (KeyError, TypeError, AttributeError) as e:
        checks.append(Check("shape", "fail", "malformed record: %s" % e))
        return checks, feed, quarantined, []
    kinds = [k for k in ("orders", "updates") if k in norm]
    checks.append(Check("shape", "pass", "%s (%s)" % (" + ".join(kinds), "v5 envelope" if "patients" in feed else "nested form")))

    # 4. sequence, per stream: replay (same bytes) is decided by the caller; here: same time + different content = conflict; older = late arrival
    for kind in kinds:
        wm = watermarks.get(kind) or {}
        last = parse_ts(wm.get("last_generated_at"))
        if last and g < last:
            checks.append(Check("sequence:%s" % kind, "fail", "generated_at %s is older than the last accepted %s file (%s): a late arrival or a stalled export; held so it cannot regress current data" % (g.strftime(ISO), kind, last.strftime(ISO))))
        elif last and g == last:
            checks.append(Check("sequence:%s" % kind, "fail", "same generated_at as the last accepted %s file but different content" % kind))
        else:
            checks.append(Check("sequence:%s" % kind, "pass", "newer than the last accepted %s file" % kind if last else "first %s file for this partner" % kind))

    # 5. schema-required fields on the v5 envelope
    req = _schema_required()
    if "patients" in feed and req:
        miss = [k for k in req.get("envelope", []) if k not in feed]
        checks.append(Check("schema", "warn", "v5 envelope missing %s (defaults apply: every capability denied)" % ", ".join(miss)) if miss else Check("schema", "pass", "envelope carries every schema-required field"))
    else:
        checks.append(Check("schema", "pass", "nested form; row checks apply"))

    cleaned = dict(norm)
    cleaned["generated_at"] = g.strftime(ISO)
    if "order_events" in cleaned:
        del cleaned["order_events"]
    offsets = _has_offset(gen)

    # 6. row checks per stream
    for kind in kinds:
        records = list(norm.get(kind) or [])
        keep: List[Dict] = []
        seen: Dict[str, str] = {}
        benign_dups = 0; conflicts = 0
        consent_absent = encoding = old = state_absent = results = 0
        codes: set = set()
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                quarantined.append({"stream": kind, "index": i, "source_id": None, "reasons": ["record is not an object"]}); continue
            if kind == "orders":
                problems, st = _check_order_row(rec, now, catalog)
                consent_absent += st["consent_absent"]; encoding += st["encoding"]; old += st["old"]; state_absent += st["state_absent"]; codes |= st["codes"]
                ident = rec.get("source_order_id") if isinstance(rec.get("source_order_id"), str) else None
                canon = _canon_order(rec) if not problems else rec
            else:
                problems, st = _check_event_row(rec, now)
                results += st["result"]
                ident = _event_id(pid, rec) if not problems else None
                canon = _canon_event(rec) if not problems else rec
            offsets = offsets or st.get("offset", False)
            if ident:
                fp = hashlib.sha1(json.dumps(canon, sort_keys=True, default=str).encode()).hexdigest()
                if ident in seen:
                    if seen[ident] == fp:
                        benign_dups += 1; continue                      # identical duplicate: dropped, not a bad row
                    conflicts += 1; problems.append("duplicate id with different content")
                else:
                    seen[ident] = fp
            if problems:
                quarantined.append({"stream": kind, "index": i, "source_id": rec.get("source_order_id") if isinstance(rec.get("source_order_id"), str) else None, "reasons": problems})
            else:
                keep.append(canon)
        n = len(records)
        bad = [q for q in quarantined if q["stream"] == kind]
        limit = max(3, int(policy.feed_bad_rows_max_fraction * (n - benign_dups)))
        if bad and (len(bad) > limit or len(bad) == n - benign_dups):
            checks.append(Check("rows:%s" % kind, "fail", "%d of %d rows failed row checks (limit: more than %d, or all); file held" % (len(bad), n, limit), bad))
        elif bad:
            checks.append(Check("rows:%s" % kind, "warn", "%d of %d rows held back; the rest import" % (len(bad), n), bad))
        else:
            checks.append(Check("rows:%s" % kind, "pass", "%d rows, all well formed" % n))
        if conflicts:
            checks.append(Check("duplicates:%s" % kind, "fail", "%d ids appear more than once with different content" % conflicts))
        elif benign_dups:
            checks.append(Check("duplicates:%s" % kind, "warn", "%d identical duplicate rows dropped (not counted as bad rows)" % benign_dups))
        else:
            checks.append(Check("duplicates:%s" % kind, "pass", "no duplicate ids within the file"))
        if kind == "orders":
            if consent_absent and n and consent_absent == n - benign_dups:
                checks.append(Check("consent", "fail", "consent_sms is missing or not boolean on every row; the consent column is probably absent from the export"))
            elif consent_absent:
                checks.append(Check("consent", "warn", "consent_sms missing on %d rows (held back)" % consent_absent))
            else:
                checks.append(Check("consent", "pass", "consent_sms present and boolean on every row"))
            checks.append(Check("encoding", "warn", "%d names look mis-encoded (mojibake or control characters); imported as sent" % encoding) if encoding else Check("encoding", "pass", "no encoding artefacts in names"))
            # identifiers: DISTINCT known patients whose normalised name AND phone both differ (churn is one of them)
            known: Dict[str, bool] = {}
            for rec in keep:
                p = rec["patient"]; spid = p["source_patient_id"]
                if spid in known:
                    continue
                ex = row(conn, "SELECT display_name, phone FROM patients WHERE partner_id=? AND source_patient_id=?", (pid, spid))
                if ex:
                    known[spid] = (_norm_phone(p.get("phone")) != _norm_phone(ex["phone"])) and (_norm_name(p.get("display_name")) != _norm_name(ex["display_name"]))
            changed = sum(1 for v in known.values() if v)
            if len(known) >= policy.feed_rekey_min_patients and changed / float(len(known)) > policy.feed_rekey_fraction:
                checks.append(Check("identifiers", "fail", "%d of %d known patients arrive with BOTH a different name and a different phone; identifiers may have been re-keyed; file held" % (changed, len(known))))
            else:
                checks.append(Check("identifiers", "pass", "%d known patients; %d changed both name and phone (below the re-key limit)" % (len(known), changed)))
            checks.append(Check("order_age", "warn", "%d orders are older than two years; they import but are probably closed elsewhere" % old) if old else Check("order_age", "pass", "order dates within range"))
            if n and state_absent == n - benign_dups:
                checks.append(Check("state_coverage", "warn", "no row carries `state`; every order is treated as open until an event says otherwise"))
            else:
                checks.append(Check("state_coverage", "pass", "order state present on %d of %d rows" % (n - benign_dups - state_absent, n - benign_dups)))
            checks.append(Check("test_codes", "warn", "test codes not in our service catalog: %s (imported; capability matching fails closed on them)" % ", ".join(sorted(codes)[:10])) if codes else Check("test_codes", "pass", "every test code is in the service catalog"))
        else:
            checks.append(Check("result_coverage", "pass" if (results or n == 0) else "warn", ("%d result/cancellation events in %d" % (results, n)) if n else "empty updates file (no changes)"))
        b = baselines.get(kind)
        nk = len(keep)
        if kind == "orders" and nk == 0:
            checks.append(Check("volume:%s" % kind, "warn", "empty orders file%s" % ("; this partner usually sends about %d rows" % round(b[0]) if b and b[0] else "")))
        elif b and b[1] >= policy.feed_baseline_min_files and b[0] > 0:
            usual = b[0]
            if nk < policy.feed_volume_drift_low * usual:
                checks.append(Check("volume:%s" % kind, "warn", "%d rows against a usual %d (below %d%%)" % (nk, round(usual), int(policy.feed_volume_drift_low * 100))))
            elif nk > policy.feed_volume_drift_high * usual:
                checks.append(Check("volume:%s" % kind, "warn", "%d rows against a usual %d (above %dx)" % (nk, round(usual), int(policy.feed_volume_drift_high))))
            else:
                checks.append(Check("volume:%s" % kind, "pass", "%d rows, usual %d" % (nk, round(usual))))
        else:
            checks.append(Check("volume:%s" % kind, "pass", "%d rows; no baseline yet (%d files)" % (len(keep), b[1] if b else 0)))
        cleaned[kind] = keep
    checks.append(Check("timezone", "warn", "timestamps carry an offset; read as the partner's wall-clock time (the contract is naive local time; confirm with the partner)") if offsets else Check("timezone", "pass", "naive local timestamps"))
    return checks, cleaned, quarantined, kinds


def verdict_of(checks: List[Check]) -> str:
    if any(c.status == "fail" for c in checks):
        return "rejected"
    if any(c.status == "warn" for c in checks):
        return "accepted_with_warnings"
    return "accepted"


# ----------------------------------------------------------------------------------------------- alert wording
# Each alert carries the exact text the partner's technical contact receives.  Specific, short, says what we did and what
# we need, and names identifiers only.  Warnings we handled ourselves are `partner_action_needed=False`: operator only.
def _msg(code: str, ctx: Dict) -> Tuple[str, str, str, bool, str]:
    """→ (severity, subject, body to partner, partner_action_needed, internal note)"""
    who = ctx.get("partner_name") or ctx.get("partner_id")
    f = ctx.get("source_name") or "the %s file" % ctx.get("kind", "feed")
    g = canon_ts(ctx.get("generated_at")) or ("an unreadable timestamp" if ctx.get("generated_at") else "no timestamp")   # never a raw value
    d = ctx.get("detail", "")
    ex = ctx.get("examples") or []
    exs = ("  Examples (ids and reasons): " + "; ".join(ex)) if ex else ""
    rid = ctx.get("receipt_id")
    if code == "file_rejected":
        if ctx.get("paused"):
            did = "the file is held unchanged; nothing from it was applied.  Outreach to your patients is paused until the next good %s file arrives, so no patient is contacted on doubtful information." % (ctx.get("kind") or "")
        else:
            did = "the file is held unchanged; nothing from it was applied.  Your most recent good file remains in use and outreach continues on it."
        return ("blocking", "%s: %s file %s (dated %s) could not be used" % (who, ctx.get("kind") or "a", f, g),
                "Hello %s technical team,\n\nWe received %s (generated %s) and could not use it: %s.%s\n\nWhat we did: %s\n\nWhat we need: a corrected export.  Nothing on our side changes until one arrives; if the export job itself failed, this message is the signal — you do not need to check anything else.  The full check list is on our receipt %s, available from StealthCo operations.\n\nStealthCo operations" % (who, f, g, d, exs, did, rid),
                True, "File held. %s" % ("Outreach paused for %s until a good %s file lands or an operator clears the block with a note." % (who, ctx.get("kind")) if ctx.get("paused") else "Current good file still in use; no block."))
    if code == "rows_quarantined":
        return ("warning", "%s: %s rows in %s held back; the rest applied" % (who, ctx.get("n"), f),
                "Hello %s technical team,\n\n%s (generated %s) imported normally except for %s rows we held back: %s.%s\n\nWhat we did: the remaining rows were applied.  The held rows are not applied, and a patient already on file whose row was held is not contacted until a clean row for them arrives.\n\nWhat we need: corrected values for these rows in a future export.  Nothing on our side repairs them.  The complete list of held rows (ids, fields, reasons) is on our receipt %s, available from StealthCo operations.\n\nStealthCo operations" % (who, f, g, ctx.get("n"), d, exs, rid),
                True, "%s rows held (receipt %s); affected known patients on hold." % (ctx.get("n"), rid))
    if code == "late":
        return ("warning", "%s: expected %s file has not arrived (last good: %s)" % (who, ctx.get("kind"), ctx.get("last") or "never"),
                "Hello %s technical team,\n\nWe expected a %s file by %s and have not received one (last good file was generated %s).\n\nWhat we did: nothing changes yet; outreach continues on the last good file until it is %d hours old, then pauses automatically.\n\nWhat we need: a check that the export job ran.  If it did and the file is in transit, ignore this; we will close it ourselves when the file lands.\n\nStealthCo operations" % (who, ctx.get("kind"), ctx.get("expected_by"), ctx.get("last") or "never", ctx.get("stale_hours", 48)),
                True, "Expected %s file missing; stale pause follows at %d h; closes itself on arrival." % (ctx.get("kind"), ctx.get("stale_hours", 48)))
    if code == "results_silent":
        return ("warning", "%s: no lab results applied from the updates stream since %s" % (who, ctx.get("since") or "the start"),
                "Hello %s technical team,\n\nOrders keep arriving, but no result or cancellation event has been applied from the updates file since %s (%s days).  That usually means the results export stopped or is filtered.\n\nWhat we did: nothing changes for patients; we keep verifying completion only from your results, so completions show as pending until this resumes.\n\nWhat we need: a look at the results side of the export.  We close this ourselves when results resume.\n\nStealthCo operations" % (who, ctx.get("since") or "the start", ctx.get("days")),
                True, "Results stream silent %s days; closes itself when a result applies." % ctx.get("days"))
    if code == "notify_failed":
        return ("info", "notification to %s failed" % who, "", False, "Could not notify the partner technical contact: %s.  Operator to send by hand." % d)
    if code == "internal_error":
        return ("info", "arrival path error on %s" % f, "", False, "The arrival path raised while handling %s: %s.  The file is retained on receipt %s; nothing was applied." % (f, d, rid))
    return ("warning", "%s: %s in %s" % (who, code.replace("_", " "), f),
            "Hello %s technical team,\n\nFYI on %s (generated %s): %s.%s  We applied the file.\n\nNo action needed.\n\nStealthCo operations" % (who, f, g, d, exs),
            False, d)


MONITOR_CODES = {"late", "results_silent"}


def open_alert(engine, partner_id: str, kind: Optional[str], code: str, ctx: Dict, receipt_id: Optional[int] = None, incident: str = "") -> int:
    """One open alert per incident.  Monitoring incidents key on code + kind + the last good file (a new outage after a
    recovery is a new incident); file-specific ones on code + kind + file name + failed checks.  Notifies the partner
    contact when partner action is needed."""
    conn = engine.conn
    now = engine.now().strftime(ISO)
    ctx = dict(ctx, partner_id=partner_id, partner_name=getattr(engine.directory, "partner_name", partner_id), kind=kind, receipt_id=receipt_id)
    severity, subject, body, action, internal = _msg(code, ctx)
    key = "%s|%s|%s" % (code, kind or "", incident if incident else subject)
    ex = row(conn, "SELECT * FROM feed_alerts WHERE partner_id=? AND dedupe_key=? AND status!='resolved'", (partner_id, key))
    if ex:
        conn.execute("UPDATE feed_alerts SET occurrences=occurrences+1, last_seen_at=?, message_internal=?, receipt_id=COALESCE(?, receipt_id) WHERE id=?", (now, internal, receipt_id, ex["id"]))
        log_event(conn, now, "system", "feed_alert_repeated", detail={"alert_id": ex["id"], "code": code, "partner_id": partner_id})
        return ex["id"]
    cur = conn.execute("INSERT INTO feed_alerts(partner_id,feed_kind,receipt_id,code,dedupe_key,severity,partner_action_needed,subject,message_to_partner,message_internal,status,opened_at,last_seen_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (partner_id, kind, receipt_id, code, key, severity, 1 if action else 0, subject, body, internal, "open", now, now))
    aid = cur.lastrowid
    log_event(conn, now, "system", "feed_alert_opened", detail={"alert_id": aid, "code": code, "severity": severity, "partner_id": partner_id, "kind": kind, "partner_action_needed": action})
    if action and body:
        contact = getattr(engine.directory, "technical_contact", {}) or {}
        try:
            r = engine.tech_contact.notify(partner_id, contact, subject, body)
            conn.execute("UPDATE feed_alerts SET notified_at=?, notify_channel=?, notify_ref=? WHERE id=?", (now, r.channel, r.ref, aid))
            log_event(conn, now, "system", "feed_partner_notified", detail={"alert_id": aid, "channel": r.channel, "ref": r.ref, "to": contact.get("email") or contact.get("name"), "simulated": r.simulated})
        except Exception as e:  # noqa: BLE001
            conn.execute("UPDATE feed_alerts SET notify_error=? WHERE id=?", (str(e), aid))
            log_event(conn, now, "system", "feed_partner_notify_failed", detail={"alert_id": aid, "error": str(e)})
            open_alert(engine, partner_id, kind, "notify_failed", {"detail": str(e)}, receipt_id, incident="alert:%d" % aid)
    return aid


def _auto_resolve(engine, partner_id: str, kind: Optional[str], code: str, resolution: str) -> int:
    now = engine.now().strftime(ISO)
    n = 0
    for a in rows(engine.conn, "SELECT id FROM feed_alerts WHERE partner_id=? AND code=? AND COALESCE(feed_kind,'')=? AND status!='resolved'", (partner_id, code, kind or "")):
        engine.conn.execute("UPDATE feed_alerts SET status='resolved', resolved_at=?, resolved_by='system', resolution=? WHERE id=?", (now, resolution, a["id"]))
        log_event(engine.conn, now, "system", "feed_alert_resolved", detail={"alert_id": a["id"], "resolution": resolution, "by": "system"})
        n += 1
    return n


def acknowledge(engine, alert_id: int, actor: str, note: str = "") -> Dict:
    if not (actor or "").strip() or actor.strip() in ("operator", "system"):
        return {"ok": False, "error": "a named actor is required to acknowledge"}
    now = engine.now().strftime(ISO)
    a = row(engine.conn, "SELECT * FROM feed_alerts WHERE id=?", (alert_id,))
    if not a:
        return {"ok": False, "error": "no such alert"}
    if a["status"] == "open":
        engine.conn.execute("UPDATE feed_alerts SET status='acknowledged', acknowledged_at=?, acknowledged_by=?, acknowledged_note=? WHERE id=?", (now, actor.strip(), note or None, alert_id))
        log_event(engine.conn, now, actor.strip(), "feed_alert_acknowledged", detail={"alert_id": alert_id, "note": note or None})
    engine.conn.commit()
    return {"ok": True, "alert": dict(row(engine.conn, "SELECT * FROM feed_alerts WHERE id=?", (alert_id,)))}


def resolve(engine, alert_id: int, actor: str, resolution: str) -> Dict:
    if not (actor or "").strip() or actor.strip() in ("operator", "system"):
        return {"ok": False, "error": "a named actor is required to resolve"}
    if not (resolution or "").strip():
        return {"ok": False, "error": "a resolution note is required"}
    now = engine.now().strftime(ISO)
    a = row(engine.conn, "SELECT * FROM feed_alerts WHERE id=?", (alert_id,))
    if not a:
        return {"ok": False, "error": "no such alert"}
    engine.conn.execute("UPDATE feed_alerts SET status='resolved', resolved_at=?, resolved_by=?, resolution=? WHERE id=?", (now, actor.strip(), resolution, alert_id))
    log_event(engine.conn, now, actor.strip(), "feed_alert_resolved", detail={"alert_id": alert_id, "resolution": resolution})
    engine.conn.commit()
    return {"ok": True}


# ----------------------------------------------------------------------------------------------- health, holds
def _health(conn, partner_id: str, kind: str) -> Dict:
    h = row(conn, "SELECT * FROM feed_health WHERE partner_id=? AND feed_kind=?", (partner_id, kind))
    return dict(h) if h else {}


def _ensure_health(conn, partner_id: str, kind: str, now: str) -> Dict:
    h = _health(conn, partner_id, kind)
    if not h:
        conn.execute("INSERT INTO feed_health(partner_id,feed_kind,status,updated_at) VALUES(?,?,?,?)", (partner_id, kind, "ok", now))
        h = _health(conn, partner_id, kind)
    return h


def _accepted(engine, partner_id: str, kind: str, generated_at: str, count: int, sha: Optional[str], train: bool = True) -> None:
    conn = engine.conn
    now = engine.now().strftime(ISO)
    h = _ensure_health(conn, partner_id, kind, now)
    n = h["baseline_n"] or 0
    b = h["baseline_count"]
    if train:
        b = float(count) if b is None or n == 0 else (b * 0.7 + float(count) * 0.3)
        n += 1
    conn.execute("UPDATE feed_health SET status='ok', blocked_reason=NULL, blocked_since=NULL, first_accepted_at=COALESCE(first_accepted_at, ?), last_accepted_at=?, last_generated_at=?, last_sha1=?, "
                 "last_record_count=?, accepted_files=accepted_files+1, consecutive_rejections=0, baseline_count=?, baseline_n=?, updated_at=? WHERE partner_id=? AND feed_kind=?",
                 (now, now, generated_at, sha, count, b, n, now, partner_id, kind))
    if h["status"] == "blocked":
        log_event(conn, now, "system", "feed_unblocked", detail={"partner_id": partner_id, "kind": kind, "by": "a good file arrived"})
    n_closed = _auto_resolve(engine, partner_id, kind, "late", "recovered: a %s file generated %s was accepted" % (kind, generated_at))
    if n_closed:
        log_event(conn, now, "system", "feed_incident_recovered", detail={"partner_id": partner_id, "kind": kind, "code": "late"})


def _rejected(engine, partner_id: str, kind: str, reason: str, block: bool) -> None:
    conn = engine.conn
    now = engine.now().strftime(ISO)
    _ensure_health(conn, partner_id, kind, now)
    if block:
        conn.execute("UPDATE feed_health SET status='blocked', blocked_reason=?, blocked_since=COALESCE(blocked_since, ?), rejected_files=rejected_files+1, "
                     "consecutive_rejections=consecutive_rejections+1, updated_at=? WHERE partner_id=? AND feed_kind=?", (reason, now, now, partner_id, kind))
        log_event(conn, now, "system", "feed_blocked", detail={"partner_id": partner_id, "kind": kind, "reason": reason})
    else:
        conn.execute("UPDATE feed_health SET rejected_files=rejected_files+1, updated_at=? WHERE partner_id=? AND feed_kind=?", (now, partner_id, kind))


def blocked(engine, partner_id: Optional[str] = None) -> Optional[str]:
    """Why outreach for this partner is blocked on feed integrity, or None."""
    ensure_schema(engine.conn)
    partner_id = partner_id or engine.directory.partner_id
    for h in rows(engine.conn, "SELECT * FROM feed_health WHERE partner_id=? AND status='blocked'", (partner_id,)):
        return "%s file: %s" % (h["feed_kind"], h["blocked_reason"])
    return None


def patient_held(conn, patient: Dict) -> Optional[str]:
    """A per-patient hold: this patient's most recent row was quarantined; nothing scheduled goes out until a clean row lands."""
    h = row(conn, "SELECT reason FROM feed_holds WHERE partner_id=? AND source_patient_id=?", (patient["partner_id"], patient["source_patient_id"]))
    return h["reason"] if h else None


def unblock(engine, partner_id: str, kind: str, actor: str, note: str) -> Dict:
    """Operator override: clears the block without a new file.  Requires a named actor and a note; logged."""
    if not (actor or "").strip() or actor.strip() in ("operator", "system"):
        return {"ok": False, "error": "a named actor is required"}
    if not (note or "").strip():
        return {"ok": False, "error": "a note is required to clear a feed block"}
    ensure_schema(engine.conn)
    now = engine.now().strftime(ISO)
    h = _health(engine.conn, partner_id, kind)
    if not h or h["status"] != "blocked":
        return {"ok": False, "error": "not blocked"}
    engine.conn.execute("UPDATE feed_health SET status='warning', blocked_reason=NULL, blocked_since=NULL, updated_at=? WHERE partner_id=? AND feed_kind=?", (now, partner_id, kind))
    log_event(engine.conn, now, actor.strip(), "feed_unblocked", detail={"partner_id": partner_id, "kind": kind, "by": "operator override", "note": note})
    engine.conn.commit()
    return {"ok": True}


# ----------------------------------------------------------------------------------------------- receive
_IMPORTER_SKIP_KINDS = ("order_rejected", "update_rejected", "update_unknown_kind", "update_for_unknown_order_pending", "update_out_of_order", "transition_rejected")


def _write_receipt(conn, ns, partner, kind, source, source_name, feed, size, sha, quarantined, verdict, applied, checks, result, raw) -> int:
    n = None
    if isinstance(feed, dict):
        n = sum(len(feed.get(k)) for k in ("orders", "updates", "order_events") if isinstance(feed.get(k), list))
    cur = conn.execute("INSERT INTO feed_receipts(partner_id,feed_kind,source,source_name,received_at,generated_at,byte_size,sha1,record_count,quarantined_count,verdict,applied,checks,import_result,quarantined) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (partner, kind, source, source_name, ns, (feed.get("generated_at") if isinstance(feed, dict) and isinstance(feed.get("generated_at"), str) else None), size, sha, n,
                        len(quarantined), verdict, 1 if applied else 0, json.dumps([c.d() for c in checks]), json.dumps(result, default=str) if result is not None else None, json.dumps(quarantined[:500])))
    rid = cur.lastrowid
    if raw is not None and source != "internal":
        conn.execute("INSERT INTO feed_payloads(receipt_id,payload,retained_at) VALUES(?,?,?)", (rid, raw, ns))
    return rid


def _newest_applied_result(conn, result: Optional[Dict], now: datetime) -> Dict:
    """The results clock moves only on evidence that APPLIED: a result/cancellation event on an order whose state actually
    changed in this import (the importer records every event in history before deciding whether to apply it)."""
    applied = ((result or {}).get("updates") or {}).get("applied") or []
    oids = [oid for oid, ch in applied if ch in ("verified_complete", "partial_result", "cancelled_by_partner")]
    if not oids:
        return {}
    return row(conn, "SELECT MAX(at) a FROM order_events WHERE kind IN ('result_finalized','cancelled') AND actor='partner_feed' AND recorded_at=? AND at<=? AND order_id IN (%s)" % ",".join("?" * len(oids)),
               (now.isoformat(), (now + timedelta(hours=1)).strftime(ISO), *oids)) or {}


def receive(engine, payload, source: str = "push", source_name: str = "") -> Dict:
    """The arrival path.  Every outcome, including an internal error, is a receipt; nothing raises to the caller.  The
    receipt of an applied import is written INSIDE the import transaction, so `rejected` can only ever mean that nothing
    was committed; an error after the commit (health, alerts) is recorded as an operator item against the committed
    receipt, never as a rejection (Codex V1 residual)."""
    ensure_schema(engine.conn)
    conn = engine.conn
    now = engine.now()
    ns = now.strftime(ISO)
    expected = engine.directory.partner_id
    feed, raw, bad = decode(payload)
    if raw is None and feed is not None:
        raw = json.dumps(feed, sort_keys=True, default=str).encode()
    sha = hashlib.sha1(raw).hexdigest()[:16] if raw is not None else None
    size = len(raw) if raw is not None else None
    committed: Dict = {}
    try:
        return _receive(engine, feed, raw, bad, sha, size, source, source_name, expected, now, ns, committed=committed)
    except Exception as e:  # noqa: BLE001 — the boundary must always leave a durable outcome
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        if committed.get("receipt_id"):
            # the import and its receipt are already committed: the truth is 'applied'; the finalisation failure is an operator item
            rid = committed["receipt_id"]
            open_alert(engine, expected, committed.get("kind"), "internal_error", {"detail": "after the import committed (receipt %d): %s: %s" % (rid, e.__class__.__name__, e), "source_name": source_name}, rid, incident="receipt:%d" % rid)
            log_event(conn, ns, "system", "feed_finalisation_failed", detail={"receipt_id": rid, "error": str(e)})
            conn.commit()
            return {"receipt_id": rid, "verdict": committed["verdict"], "kind": committed.get("kind"), "partner_id": committed.get("partner"), "checks": committed.get("checks", []),
                    "quarantined": committed.get("quarantined", 0), "import_result": committed.get("result"), "applied": True, "holds": committed.get("holds", {}), "finalisation_error": str(e)}
        checks = [Check("internal_error", "fail", "%s: %s" % (e.__class__.__name__, e))]
        rid = _write_receipt(conn, ns, (feed or {}).get("partner_id") if isinstance(feed, dict) and isinstance((feed or {}).get("partner_id"), str) else None,
                             "unknown", source, source_name, feed if isinstance(feed, dict) else None, size, sha, [], "rejected", False, checks, None, raw)
        log_event(conn, ns, "system", "feed_received", detail={"receipt_id": rid, "source": source, "source_name": source_name, "verdict": "rejected", "failed": ["internal_error"], "error": str(e)})
        open_alert(engine, expected, None, "internal_error", {"detail": str(e), "source_name": source_name}, rid, incident="receipt:%d" % rid)
        conn.commit()
        return {"receipt_id": rid, "verdict": "rejected", "kind": "unknown", "partner_id": None, "checks": [c.d() for c in checks], "quarantined": 0, "import_result": None, "applied": False}


def _receive(engine, feed, raw, bad, sha, size, source, source_name, expected, now, ns, mode: str = "arrival", committed: Optional[Dict] = None) -> Dict:
    committed = committed if committed is not None else {}
    conn = engine.conn
    pol = engine.policy
    quarantined: List[Dict] = []
    result = None
    applied = False
    kinds: List[str] = []
    partner = feed.get("partner_id") if isinstance(feed, dict) and isinstance(feed.get("partner_id"), str) else None
    # replay: the same bytes as a file already accepted for this partner → a NO-OP on state, decided before anything else (V2)
    if mode == "arrival" and sha and partner and row(conn, "SELECT 1 FROM feed_receipts WHERE partner_id=? AND sha1=? AND applied=1 AND source!='internal'", (partner, sha)):
        checks = [Check("replay", "pass", "identical to a file already accepted; nothing imported, no health, baseline, alert or hold change")]
        rid = _write_receipt(conn, ns, partner, "replay", source, source_name, feed, size, sha, [], "accepted_replay", False, checks, None, raw)
        log_event(conn, ns, "system", "feed_received", detail={"receipt_id": rid, "partner_id": partner, "kind": "replay", "source": source, "source_name": source_name, "verdict": "accepted_replay", "applied": False})
        conn.commit()
        return {"receipt_id": rid, "verdict": "accepted_replay", "kind": "replay", "partner_id": partner, "checks": [c.d() for c in checks], "quarantined": 0, "import_result": None, "applied": False, "holds": {"held": 0, "cleared": 0}}
    if bad:
        checks = [bad]
        cleaned = None
    else:
        if mode == "reprocess":
            feed = dict(feed)
            for k in ("orders", "patients"):
                feed.pop(k, None)                                  # an operator reprocess applies EVENTS only; demographics are never re-written from an old file
            if "updates" not in feed and "order_events" not in feed:
                feed["updates"] = []
        watermarks = {k: _health(conn, partner, k) for k in ("orders", "updates")} if partner else {}
        baselines = {k: (h.get("baseline_count") or 0.0, h.get("baseline_n") or 0) for k, h in watermarks.items() if h}
        checks, cleaned, quarantined, kinds = validate(feed, expected, now, conn, pol, catalog=getattr(engine, "catalog", None), watermarks=watermarks, baselines=baselines)
        if mode == "reprocess":
            checks = [c for c in checks if not c.check.startswith("sequence:")] + [Check("reprocess", "pass", "operator reprocess: events stream only, sequence waived, orders stream skipped, health untouched")]
    kind = "+".join(kinds) if kinds else "unknown"
    verdict = verdict_of(checks)
    hold_log = {"held": 0, "cleared": 0}
    if verdict != "rejected" and cleaned is not None:
        before = row(conn, "SELECT COALESCE(MAX(id),0) m FROM events")["m"]

        rid_box: Dict = {}

        def finalise(_result):
            """Inside the import transaction (V1): holds, importer-skip collection and the receipt itself, so a receipt that
            says `applied` is committed with the import and a receipt that says `rejected` means nothing was committed."""
            nonlocal verdict
            if "orders" in kinds:
                for rec in cleaned.get("orders") or []:
                    if conn.execute("DELETE FROM feed_holds WHERE partner_id=? AND source_patient_id=?", (partner, rec["patient"]["source_patient_id"])).rowcount:
                        hold_log["cleared"] += 1
                src_rows = feed.get("orders") if isinstance(feed.get("orders"), list) else []
                for q in quarantined:
                    if q.get("stream") != "orders" or q.get("index") is None or q["index"] >= len(src_rows):
                        continue
                    rec = src_rows[q["index"]]
                    if not isinstance(rec, dict):
                        continue                                       # a non-object row names nobody; nothing to hold
                    pat = rec.get("patient") if isinstance(rec.get("patient"), dict) else {}
                    spid = pat.get("source_patient_id") if isinstance(pat.get("source_patient_id"), str) else (rec.get("source_patient_id") if isinstance(rec.get("source_patient_id"), str) else None)
                    if spid and row(conn, "SELECT 1 FROM patients WHERE partner_id=? AND source_patient_id=?", (partner, spid)):
                        conn.execute("INSERT OR REPLACE INTO feed_holds(partner_id,source_patient_id,reason,receipt_id,since) VALUES(?,?,?,?,?)", (partner, spid, "; ".join(q.get("reasons", [])), None, ns))
                        hold_log["held"] += 1
            for ev in rows(conn, "SELECT kind, detail FROM events WHERE id>? AND kind IN (%s)" % ",".join("?" * len(_IMPORTER_SKIP_KINDS)), (before, *_IMPORTER_SKIP_KINDS)):
                d = json.loads(ev["detail"] or "{}")
                quarantined.append({"stream": "updates" if ev["kind"].startswith("update") or ev["kind"] == "transition_rejected" else "orders",
                                    "index": None, "source_id": d.get("source_order_id") or d.get("event_id"), "reasons": ["%s: %s" % (ev["kind"], d.get("reason") or d.get("note") or d.get("error") or "")]})
            imp_q = [q for q in quarantined if q.get("index") is None]
            if imp_q:
                checks.append(Check("import", "warn", "%d records the importer could not apply (unknown order, unknown codes, out of order); listed as held" % len(imp_q), imp_q))
                if verdict == "accepted":
                    verdict = "accepted_with_warnings"
            rid_box["rid"] = _write_receipt(conn, ns, partner, kind, source, source_name, feed, size, sha, quarantined, verdict, True, checks, _result, raw)
            if hold_log["held"]:
                conn.execute("UPDATE feed_holds SET receipt_id=? WHERE receipt_id IS NULL AND partner_id=?", (rid_box["rid"], partner))
                log_event(conn, ns, "system", "feed_patients_held", detail={"receipt_id": rid_box["rid"], "held": hold_log["held"], "cleared": hold_log["cleared"]})
        try:
            result = engine.import_validated(cleaned, source_name=source_name or source, before_commit=finalise)
            applied = True
            committed.update({"receipt_id": rid_box["rid"], "verdict": verdict, "kind": kind, "partner": partner, "checks": [c.d() for c in checks], "quarantined": len(quarantined), "result": result, "holds": dict(hold_log)})
        except FeedRejected as e:
            checks.append(Check("import", "fail", "importer refused the file: %s" % e)); verdict = "rejected"; hold_log = {"held": 0, "cleared": 0}
        except Exception as e:  # noqa: BLE001
            checks.append(Check("import", "fail", "import failed and was rolled back: %s: %s" % (e.__class__.__name__, e))); verdict = "rejected"; hold_log = {"held": 0, "cleared": 0}
    rid = committed.get("receipt_id") if applied else _write_receipt(conn, ns, partner, kind, source, source_name, feed, size, sha, quarantined, verdict, False, checks, result, raw)
    log_event(conn, ns, "system", "feed_received", detail={"receipt_id": rid, "partner_id": partner, "kind": kind, "source": source, "source_name": source_name, "verdict": verdict, "applied": applied,
                                                          "failed": [c.check for c in checks if c.status == "fail"], "warned": [c.check for c in checks if c.status == "warn"]})
    gen = feed.get("generated_at") if isinstance(feed, dict) and isinstance(feed.get("generated_at"), str) else None
    if verdict == "rejected":
        failed = [c for c in checks if c.status == "fail"]
        why = "; ".join("%s: %s" % (c.check, c.detail) for c in failed)
        late_only = bool(kinds) and all(c.check.startswith("sequence:") and "older" in c.detail for c in failed)
        current = all((lambda h: parse_ts(h.get("last_generated_at")) and now - parse_ts(h["last_generated_at"]) <= timedelta(hours=pol.stale_feed_hours))(_health(conn, expected, k)) for k in kinds)
        block = bool(kinds) and mode == "arrival" and not (late_only and current)      # a late arrival behind a CURRENT good file does not block; behind a stale one it is a stalled export
        for k in kinds:
            _rejected(engine, expected, k, why, block=block)
        examples = ["%s: %s" % (q.get("source_id") or "row %s" % q.get("index"), ", ".join(q.get("reasons", []))) for q in quarantined[:3]]
        open_alert(engine, expected, kind if kind != "unknown" else None, "file_rejected",
                   {"detail": why, "source_name": source_name, "generated_at": gen, "examples": examples, "paused": block},
                   rid, incident="%s|%s" % (source_name or sha, ",".join(c.check for c in failed)))
    else:
        for k in kinds:
            n = len(cleaned.get(k) or [])
            if mode == "arrival":
                train = not any(c.check == "volume:%s" % k and c.status == "warn" for c in checks)     # an anomalous file never trains the baseline
                _accepted(engine, partner, k, cleaned["generated_at"], n, sha, train=train)
            if k == "updates":
                newest = _newest_applied_result(conn, result, now)
                if newest.get("a"):
                    conn.execute("UPDATE feed_health SET last_result_event_at=MAX(COALESCE(last_result_event_at,''), ?) WHERE partner_id=? AND feed_kind='updates'", (newest["a"], partner))
                    _auto_resolve(engine, partner, "updates", "results_silent", "recovered: a result event dated %s was applied" % newest["a"])
        for c in checks:
            if c.status != "warn":
                continue
            base = c.check.split(":")[0]
            ck = c.check.split(":")[1] if ":" in c.check else kind
            ctx = {"detail": c.detail, "source_name": source_name, "generated_at": gen}
            if base in ("rows", "import"):
                qs = [q for q in quarantined if (q.get("stream") == ck if base == "rows" else q.get("index") is None)]
                ctx.update({"n": len(qs), "examples": ["%s: %s" % (q.get("source_id") or "row %s" % q.get("index"), ", ".join(q.get("reasons", []))) for q in qs[:3]]})
                open_alert(engine, partner, ck, "rows_quarantined", ctx, rid, incident="%s|%s" % (source_name or sha, c.check))
            else:
                open_alert(engine, partner, ck if ck in ("orders", "updates") else kind, base, ctx, rid, incident=base)
    conn.commit()
    return {"receipt_id": rid, "verdict": verdict, "kind": kind, "partner_id": partner, "checks": [c.d() for c in checks],
            "quarantined": len(quarantined), "import_result": result, "applied": applied, "holds": hold_log}


def reprocess(engine, receipt_id: int, actor: str) -> Dict:
    """A named operator re-runs the EVENTS stream of an earlier receipt from its retained payload (deferred events whose
    orders have since arrived).  The orders stream is never re-applied: demographics from an old file must not overwrite
    newer partner corrections (V2).  Health, watermark and baseline are untouched."""
    ensure_schema(engine.conn)
    if not (actor or "").strip() or actor.strip() in ("operator", "system"):
        return {"ok": False, "error": "a named actor is required"}
    p = row(engine.conn, "SELECT r.source_name, p.payload FROM feed_receipts r JOIN feed_payloads p ON p.receipt_id=r.id WHERE r.id=?", (receipt_id,))
    if not p:
        return {"ok": False, "error": "no retained payload for that receipt"}
    now = engine.now(); ns = now.strftime(ISO)
    log_event(engine.conn, ns, actor.strip(), "feed_reprocess", detail={"receipt_id": receipt_id})
    feed, raw, bad = decode(bytes(p["payload"]))
    sha = hashlib.sha1(raw).hexdigest()[:16] if raw is not None else None
    name = "%s (reprocess of receipt %d)" % (p["source_name"] or "", receipt_id)
    try:
        r = _receive(engine, feed, raw, bad, sha, len(raw) if raw else None, "reprocess", name, engine.directory.partner_id, now, ns, mode="reprocess")
    except Exception as e:  # noqa: BLE001
        engine.conn.rollback()
        return {"ok": False, "error": "%s: %s" % (e.__class__.__name__, e)}
    return dict(r, ok=True)


def record_internal(engine, feed: Dict, kind: str, source_name: str, result: Optional[Dict], error: Optional[str]) -> None:
    """The trusted local path (seed, scenarios, tests) still leaves a receipt and keeps health current, so the operator
    surface shows every file the system has ever consumed.  No partner alerts: these files never came from a partner."""
    ensure_schema(engine.conn)
    conn = engine.conn
    ns = engine.now().strftime(ISO)
    partner = feed.get("partner_id") if isinstance(feed.get("partner_id"), str) else None
    checks = [Check("trusted_path", "pass" if not error else "fail", "local file consumed by the importer directly" if not error else error)]
    rid = _write_receipt(conn, ns, partner, kind, "internal", source_name, feed, None, None, [], "accepted" if not error else "rejected", not error, checks, result, None)
    if partner and not error and isinstance(feed.get("generated_at"), str):
        n = len(feed.get("orders") or feed.get("updates") or feed.get("order_events") or [])
        _accepted(engine, partner, kind, canon_ts(feed["generated_at"]) or feed["generated_at"], n, None, train=True)
        if kind == "updates":
            newest = _newest_applied_result(conn, {"updates": result or {}}, engine.now())
            if newest.get("a"):
                conn.execute("UPDATE feed_health SET last_result_event_at=MAX(COALESCE(last_result_event_at,''), ?) WHERE partner_id=? AND feed_kind='updates'", (newest["a"], partner))
    return rid


# ----------------------------------------------------------------------------------------------- scheduler tick
def tick(engine) -> List[str]:
    """Pick up waiting files; notice what did not arrive; wire the integrity block to the outreach pause.  Runs when the
    scheduler tick runs; nothing here schedules itself."""
    ensure_schema(engine.conn)
    out: List[str] = []
    now = engine.now()
    src = getattr(engine, "feed_source", None)
    if src is not None:
        try:
            names = src.pending()
        except Exception as e:  # noqa: BLE001
            log_event(engine.conn, now.strftime(ISO), "system", "feed_pull_failed", detail={"error": "pending(): %s" % e}); names = []
            out.append("feed_pull_failed:pending")
        for name in names:
            try:
                claimed = src.claim(name)
                r = receive(engine, src.read(claimed), source="pull", source_name=name.split(".claimed-")[0])
                src.done(claimed, r["verdict"])
                out.append("feed_pulled:%s:%s" % (name.split(".claimed-")[0], r["verdict"]))
            except Exception as e:  # noqa: BLE001
                log_event(engine.conn, now.strftime(ISO), "system", "feed_pull_failed", detail={"file": name, "error": str(e)})
                out.append("feed_pull_failed:%s" % name)
    pid = engine.directory.partner_id
    pol = engine.policy
    ho, hu = _health(engine.conn, pid, "orders"), _health(engine.conn, pid, "updates")
    due = timedelta(hours=pol.feed_expected_every_hours + pol.feed_late_grace_hours)
    for kind, h in (("orders", ho), ("updates", hu)):
        last = parse_ts(h.get("last_generated_at")) if h else None
        if last is None:
            # a stream that never started is measured from the other stream's first file
            other = hu if kind == "orders" else ho
            start = parse_ts(other.get("first_accepted_at")) if other else None
            if start and now - start > due:
                open_alert(engine, pid, kind, "late", {"expected_by": (start + due).strftime(ISO), "last": None, "stale_hours": pol.stale_feed_hours}, incident="never")
                out.append("feed_late:%s" % kind)
            continue
        if now - last > due:
            open_alert(engine, pid, kind, "late", {"expected_by": (last + due).strftime(ISO), "last": h["last_generated_at"], "stale_hours": pol.stale_feed_hours}, incident=h["last_generated_at"])
            out.append("feed_late:%s" % kind)
    if ho.get("last_accepted_at") and hu.get("accepted_files", 0) >= 1:
        orders_current = parse_ts(ho.get("last_generated_at")) and now - parse_ts(ho["last_generated_at"]) <= timedelta(hours=pol.stale_feed_hours)
        since = hu.get("last_result_event_at") or hu.get("first_accepted_at")
        s = parse_ts(since) if since else None
        if orders_current and s and (now - s).days >= pol.feed_results_silent_days:
            open_alert(engine, pid, "updates", "results_silent", {"since": since, "days": (now - s).days}, incident=since)
            out.append("feed_results_silent")
    why = blocked(engine, pid)
    pr = engine.paused()
    if why and (not pr or pr == "policy:stale_feed"):
        set_setting(engine.conn, "pause_reason", PAUSE_REASON)
        log_event(engine.conn, now.strftime(ISO), "system", "paused", detail={"reason": "feed_integrity", "why": why})
        out.append("paused:feed_integrity")
    elif not why and pr == PAUSE_REASON:
        engine.resume(actor="system")
        out.append("resumed:feed_integrity_clear")
    return out


# ----------------------------------------------------------------------------------------------- surfaces
def payload(engine) -> Dict:
    ensure_schema(engine.conn)
    pid = engine.directory.partner_id
    health = [dict(h) for h in rows(engine.conn, "SELECT * FROM feed_health ORDER BY partner_id, feed_kind")]
    alerts = [dict(a) for a in rows(engine.conn, "SELECT * FROM feed_alerts WHERE status!='resolved' ORDER BY CASE severity WHEN 'blocking' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, opened_at DESC")]
    receipts = []
    for r in rows(engine.conn, "SELECT r.*, (p.receipt_id IS NOT NULL) has_payload FROM feed_receipts r LEFT JOIN feed_payloads p ON p.receipt_id=r.id ORDER BY r.id DESC LIMIT 12"):
        d = dict(r); d["checks"] = json.loads(d["checks"] or "[]"); d["quarantined"] = json.loads(d.get("quarantined") or "[]"); receipts.append(d)
    contact = getattr(engine.directory, "technical_contact", {}) or {}
    pol = engine.policy
    return {"partner_id": pid, "blocked": blocked(engine, pid), "health": health, "open_alerts": alerts, "receipts": receipts,
            "holds": [dict(h) for h in rows(engine.conn, "SELECT * FROM feed_holds ORDER BY since DESC")],
            "technical_contact": {k: contact.get(k) for k in ("name", "email", "channel", "role")},
            "notifier": {"adapter": getattr(engine.tech_contact, "name", "?"), "simulated": bool(getattr(engine.tech_contact, "simulated", True))},
            "source": {"adapter": getattr(engine.feed_source, "name", "none"), "path": getattr(engine.feed_source, "path", None), "simulated": True,
                       "note": "the pickup runs when the scheduler tick runs; nothing schedules itself in this prototype; delivery is at-least-once with idempotent replay"},
            "policy": {k: getattr(pol, k) for k in ("feed_expected_every_hours", "feed_late_grace_hours", "stale_feed_hours", "feed_results_silent_days", "feed_bad_rows_max_fraction",
                                                    "feed_rekey_fraction", "feed_rekey_min_patients", "feed_volume_drift_low", "feed_volume_drift_high", "feed_baseline_min_files")},
            "timestamp_contract": "ISO 8601; offsets accepted and read as the partner's wall-clock time (naive local); flagged by the `timezone` check",
            "principle": "never outsource to the health system's technology team work we can do ourselves: we validate every file on arrival, we notice what did not arrive, and we tell their contact something specific."}


def summary(engine) -> Dict:
    ensure_schema(engine.conn)
    pid = engine.directory.partner_id
    a = row(engine.conn, "SELECT SUM(CASE WHEN severity='blocking' THEN 1 ELSE 0 END) b, COUNT(*) n FROM feed_alerts WHERE status!='resolved'") or {}
    r = row(engine.conn, "SELECT COUNT(*) n, SUM(CASE WHEN verdict='rejected' THEN 1 ELSE 0 END) rej, SUM(quarantined_count) q FROM feed_receipts") or {}
    return {"blocked": blocked(engine, pid), "open_alerts": a.get("n") or 0, "open_blocking": a.get("b") or 0,
            "files_received": r.get("n") or 0, "files_rejected": r.get("rej") or 0, "rows_quarantined": r.get("q") or 0,
            "patients_held": row(engine.conn, "SELECT COUNT(*) n FROM feed_holds")["n"]}
