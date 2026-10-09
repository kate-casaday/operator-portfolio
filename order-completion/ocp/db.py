"""SQLite persistence.  One file, WAL off (simplicity), foreign keys on."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patients (
  id INTEGER PRIMARY KEY,
  partner_id TEXT NOT NULL,
  source_patient_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  phone TEXT,
  language TEXT DEFAULT 'en',
  consent_sms INTEGER DEFAULT 0,
  partner_consent_sms INTEGER DEFAULT 0,
  local_opt_out INTEGER NOT NULL DEFAULT 0,
  phone_ambiguous INTEGER NOT NULL DEFAULT 0,
  synthetic INTEGER NOT NULL DEFAULT 0,  -- 1 only when the partner feed declared "synthetic": true; export requires it
  home_town TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(partner_id, source_patient_id)
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  partner_id TEXT NOT NULL,
  source_order_id TEXT NOT NULL,
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  ordered_at TEXT NOT NULL,
  intended_due_at TEXT,                 -- optional; an order is not overdue before its intended date
  claim_location TEXT,                  -- patient-reported completion location (model interpretation, <=40 chars)
  claim_in_network INTEGER,             -- 1 partner lab / 0 elsewhere / NULL unknown (patient statement via keywords or model)
  ordering_provider TEXT,
  priority TEXT DEFAULT 'routine',
  state TEXT NOT NULL,
  state_updated_at TEXT NOT NULL,
  verified_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(partner_id, source_order_id)
);
CREATE TABLE IF NOT EXISTS order_lines (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  test_code TEXT NOT NULL,
  test_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'outstanding',   -- outstanding | resulted | cancelled
  resulted_at TEXT,
  UNIQUE(order_id, test_code)
);
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL UNIQUE REFERENCES patients(id),
  state TEXT NOT NULL,
  state_updated_at TEXT NOT NULL,
  outreach_attempts INTEGER NOT NULL DEFAULT 0,   -- monotonic: every outreach ever sent in this episode
  reduced_allowance INTEGER,                       -- remaining reminders after "fewer reminders" (NULL = normal cadence)
  next_action_at TEXT,
  next_action TEXT,
  next_action_reason TEXT,
  agreed_site_id TEXT,
  agreed_when TEXT,
  agreed_date TEXT,
  pending_site_id TEXT,
  epoch INTEGER NOT NULL DEFAULT 0,
  episode INTEGER NOT NULL DEFAULT 1,   -- outreach episode; increments on reopen so cadence dedupe keys are new
  closed_reason TEXT,
  paused_reason TEXT,
  model_calls INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  outbound_count INTEGER NOT NULL DEFAULT 0,
  inbound_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  direction TEXT NOT NULL,              -- inbound | outbound
  kind TEXT NOT NULL DEFAULT 'reply',   -- compliance | safety | reply | scheduled
  epoch INTEGER NOT NULL DEFAULT 0,     -- conversation epoch when queued; stale epoch => cancelled at flush
  to_phone TEXT,
  from_phone TEXT,                      -- inbound: original sender number (immutable; recovery binds to this)
  site_ids TEXT,
  content_deps TEXT,                    -- JSON {"sites":{id:verified_at},"instructions":{key:approved_at}} revalidated at send
  decision TEXT,                        -- JSON: why the application chose this outbound (rule, intent, constraints, sites, reason)                        -- JSON list of directory site ids the body depends on (revalidated at send)
  body TEXT NOT NULL,
  template_id TEXT,
  segments INTEGER NOT NULL DEFAULT 1,
  provider TEXT,
  provider_message_id TEXT UNIQUE,
  dedupe_key TEXT UNIQUE,
  status TEXT NOT NULL,                 -- inbound: received | processed | error   outbound: queued | sending | sent | failed | suppressed | cancelled | ambiguous
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  sent_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,                  -- system | model:<name> | kate | partner_feed | patient | clinician
  kind TEXT NOT NULL,
  patient_id INTEGER,
  order_id INTEGER,
  conversation_id INTEGER,
  detail TEXT                           -- JSON
);
CREATE TABLE IF NOT EXISTS escalations (
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  order_id INTEGER,
  reason TEXT NOT NULL,
  queue TEXT NOT NULL,                  -- kate | clinician
  after_hours INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',  -- open | resolved
  assigned_to TEXT,
  due_at TEXT,
  acknowledged_at TEXT,
  overdue INTEGER NOT NULL DEFAULT 0,
  handoff_status TEXT NOT NULL DEFAULT 'queued_simulated',   -- queued_simulated | notified | accepted
  opened_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by TEXT,
  resolution TEXT,
  human_minutes REAL NOT NULL DEFAULT 0,
  minutes_source TEXT                   -- logged | default_assumed | none
);
CREATE TABLE IF NOT EXISTS preferences (
  id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  source TEXT NOT NULL,                 -- patient_statement | model_interpretation | kate | partner_feed
  message_id INTEGER,                   -- the patient message the value was taken from (the statement itself)
  corrected INTEGER NOT NULL DEFAULT 0, -- 1 when this value replaced a different earlier value
  superseded_by INTEGER,                -- id of the newer preference row, if any
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  conversation_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  label TEXT NOT NULL,
  should_have TEXT NOT NULL,
  notes TEXT,
  evidence TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  linked_ref TEXT,
  verification_note TEXT,
  archived INTEGER NOT NULL DEFAULT 0,  -- 1 after a fresh session reset: joins to live rows are meaningless, evidence JSON is authoritative
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS suppressed_numbers (
  phone TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_events (
  event_id TEXT PRIMARY KEY,
  partner_id TEXT NOT NULL,
  source_order_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  at TEXT,
  received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_imports (
  id INTEGER PRIMARY KEY,
  partner_id TEXT NOT NULL,
  feed_kind TEXT NOT NULL,              -- orders | updates
  generated_at TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  record_count INTEGER NOT NULL,
  source_name TEXT
);
CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  conversation_id INTEGER,
  adapter TEXT NOT NULL,
  model TEXT NOT NULL,
  simulated INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms REAL NOT NULL,
  outcome TEXT NOT NULL,                -- ok | error | fallback
  intent TEXT,
  confidence REAL
);
CREATE TABLE IF NOT EXISTS human_time (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  activity TEXT NOT NULL,
  minutes REAL NOT NULL,
  source TEXT NOT NULL DEFAULT 'logged',   -- logged | default_assumed
  escalation_id INTEGER
);
CREATE TABLE IF NOT EXISTS patient_locations (
  id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  source TEXT NOT NULL,                 -- feed_address | patient_town | patient_zip | patient_shared | kate
  label TEXT,                           -- the town / zip / "shared from phone" (never a street address)
  consent_note TEXT,                    -- how the patient authorized it (the message id, or the share page)
  created_at TEXT NOT NULL,
  expires_at TEXT
);
CREATE TABLE IF NOT EXISTS location_links (
  token TEXT PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
CREATE TABLE IF NOT EXISTS portal_messages (
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  patient_id INTEGER NOT NULL,
  inbound_message_id INTEGER,           -- the patient text that IS the message (verbatim; never model-written)
  provider TEXT,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  order_refs TEXT,                      -- JSON list of source_order_id
  adapter TEXT NOT NULL,                -- simulated | <vendor>
  status TEXT NOT NULL,                 -- sending | sent | sent_simulated | failed | ambiguous (crash between call and record)
  provider_message_id TEXT,
  created_at TEXT NOT NULL,
  sent_at TEXT,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS order_events (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  patient_id INTEGER NOT NULL,
  event_id TEXT,
  kind TEXT NOT NULL,                   -- imported | result_finalized | cancelled | replaced | modified | attended | state | patient_reported_change
  at TEXT,
  actor TEXT NOT NULL,                  -- partner_feed | patient | system | kate
  detail TEXT,
  recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_patient ON orders(patient_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_events_conv ON events(conversation_id);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open (and initialize) the database at `path`.  ':memory:' is allowed for tests."""
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after v0.3.  `CREATE TABLE IF NOT EXISTS` leaves existing databases untouched, so each is
# added here when missing (SQLite has no ADD COLUMN IF NOT EXISTS).
_MIGRATIONS = [
    ("conversations", "opener_variant", "TEXT"),          # JSON: which first-text variant this conversation received
    ("orders", "visit_at", "TEXT"),                        # the visit the order came from, when the partner feed carries it
    ("model_calls", "purpose", "TEXT NOT NULL DEFAULT 'classify'"),   # classify | compose
    ("model_calls", "cost_usd", "REAL NOT NULL DEFAULT 0"),           # priced at the attempt's own model; 0 for simulated
    ("model_calls", "wall_at", "TEXT"),                    # real wall-clock time (spend caps are about real money, not the sim clock)
    ("messages", "composer", "TEXT"),                      # fact | anthropic | template (fallback) — who wrote the words
    ("conversations", "relay_pending", "TEXT"),            # JSON {kind, message_id, at} while a portal relay offer awaits the patient's message (v4)
    ("conversations", "clinical_referrals", "INTEGER NOT NULL DEFAULT 0"),   # portal referrals sent in this conversation (v4)
    ("escalations", "priority", "TEXT NOT NULL DEFAULT 'normal'"),           # normal | emergency (v4)
    ("model_calls", "conversation_purpose", "TEXT"),       # reserved
    ("portal_messages", "context_question", "TEXT"),       # the patient's original clinical text that led to the relay offer (V4-2)
    ("orders", "superseded_by", "INTEGER"),                # v5: the replacement order's id when a provider event replaced this one
    ("orders", "care_team", "TEXT"),                       # v5: JSON {ordering_clinician, pcp, episode_team, ambiguous}
    ("conversations", "pause_reason", "TEXT"),             # v5: why outreach is quiet (clinical_referral | relay_sent | plan_change_reported | emergency)
    ("conversations", "pending_slots", "TEXT"),            # v5: JSON offered booking slots awaiting the patient's pick
    ("conversations", "booking_id", "INTEGER"),            # v5: active booking
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, decl in _MIGRATIONS:
        have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
        if col not in have:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
    conn.commit()


_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", re.I)


def ensure_tables(conn: sqlite3.Connection, script: str) -> bool:
    """Run a CREATE TABLE script only when one of its tables is missing.  `executescript` issues a COMMIT first, so
    calling it inside a business transaction would commit half an import (Codex, feed-integrity review finding 1).
    Engine.__init__ creates every module's tables up front; afterwards this is a read of sqlite_master and nothing else."""
    names = _TABLE_RE.findall(script)
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if all(n in have for n in names):
        return False
    if conn.in_transaction:
        raise RuntimeError("schema for %s missing inside a transaction; create tables at startup" % [n for n in names if n not in have])
    conn.executescript(script)
    return True


def get_setting(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
    conn.commit()


def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def row(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def log_event(conn: sqlite3.Connection, at: str, actor: str, kind: str, patient_id=None, order_id=None,
              conversation_id=None, detail: Optional[Dict[str, Any]] = None) -> int:
    cur = conn.execute(
        "INSERT INTO events(at,actor,kind,patient_id,order_id,conversation_id,detail) VALUES(?,?,?,?,?,?,?)",
        (at, actor, kind, patient_id, order_id, conversation_id, json.dumps(detail or {}, sort_keys=True)))
    return cur.lastrowid
