"""Version 5: a SIMULATED scheduling adapter and the bookings ledger (v5 brief §6).

Four outcomes are tracked separately and never conflated: a patient's intention / walk-in plan (conversations.agreed_*),
a confirmed booking (bookings.status='booked'), attendance (bookings.status='attended', from a partner event), and
verified completion (orders.state='verified_complete', from a partner result).  A walk-in plan is never called an
appointment.  Every capability here is simulated; a partner scheduling integration is a partner decision.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .db import row, rows, log_event, ensure_tables

ISO = "%Y-%m-%dT%H:%M:%S"
STATUSES = ("booked", "rescheduled", "cancelled", "cancel_pending", "attended", "no_show", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  patient_id INTEGER NOT NULL,
  order_ids TEXT,
  site_id TEXT NOT NULL,
  slot_at TEXT NOT NULL,
  confirmation_id TEXT,
  adapter TEXT NOT NULL,                -- simulated | <partner>
  status TEXT NOT NULL,                 -- booked | rescheduled | cancelled | attended | no_show | failed
  previous_booking_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_error TEXT
);
"""


def ensure_schema(conn) -> None:
    ensure_tables(conn, SCHEMA)          # never executescript mid-transaction (it commits)


class SchedulingError(Exception):
    """The scheduler definitely did not book / cancel / move the slot."""


class SimulatedScheduler:
    """Deterministic availability from a site's verified hours: one slot per hour, a stable subset marked taken.  Nothing
    leaves the process.  `fail_times` injects failures for tests."""
    name = "simulated"
    simulated = True

    def __init__(self, directory, fail_times: int = 0, lead_time_hours: int = 2):
        self.directory = directory
        self.fail_times = fail_times
        self.lead_time_hours = lead_time_hours
        self.booked: Dict[str, Dict] = {}

    def bookable(self, site: Dict) -> bool:
        sch = site.get("scheduling") or {}
        return sch.get("adapter") == "simulated"

    def availability(self, site: Dict, day: datetime, now: datetime, limit: int = 3) -> List[str]:
        """ISO slot times on `day` at `site`, after the lead time, not taken."""
        if not self.bookable(site):
            return []
        wd = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][day.weekday()]
        span = site["hours"].get(wd)
        if not span:
            return []
        out = []
        h0, h1 = int(span[0][:2]), int(span[1][:2])
        for h in range(h0, h1):
            slot = day.replace(hour=h, minute=0, second=0, microsecond=0)
            if slot < now + timedelta(hours=self.lead_time_hours):
                continue
            key = "%s|%s" % (site["id"], slot.strftime(ISO))
            taken = int(hashlib.sha1(key.encode()).hexdigest(), 16) % 3 == 0     # a third of slots are "taken", stably
            if taken or key in self.booked:
                continue
            out.append(slot.strftime(ISO))
            if len(out) >= limit:
                break
        return out

    def book(self, site_id: str, slot_at: str, patient_ref: str, order_refs: List[str]) -> str:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise SchedulingError("simulated scheduler failure")
        key = "%s|%s" % (site_id, slot_at)
        if key in self.booked:
            raise SchedulingError("slot no longer available")
        conf = "SIMBK-" + uuid.uuid4().hex[:8].upper()
        self.booked[key] = {"confirmation_id": conf, "patient_ref": patient_ref, "order_refs": list(order_refs)}
        return conf

    def cancel(self, site_id: str, slot_at: str, confirmation_id: str) -> None:
        key = "%s|%s" % (site_id, slot_at)
        b = self.booked.get(key)
        if not b or b["confirmation_id"] != confirmation_id:
            raise SchedulingError("unknown booking")
        del self.booked[key]

    def reschedule(self, site_id: str, old_slot: str, confirmation_id: str, new_slot: str, patient_ref: str, order_refs: List[str]) -> str:
        self.cancel(site_id, old_slot, confirmation_id)
        return self.book(site_id, new_slot, patient_ref, order_refs)


# ------------------------------------------------------------------------------------------------ ledger helpers
def active_booking(conn, conversation_id: int) -> Optional[Dict]:
    ensure_schema(conn)
    return row(conn, "SELECT * FROM bookings WHERE conversation_id=? AND status IN ('booked','rescheduled','cancel_pending') ORDER BY id DESC LIMIT 1", (conversation_id,))


def record(conn, now: datetime, conversation_id: int, patient_id: int, order_ids: List[int], site_id: str, slot_at: str, confirmation_id: Optional[str],
           adapter: str, status: str, previous: Optional[int] = None, error: Optional[str] = None) -> int:
    ensure_schema(conn)
    import json
    cur = conn.execute("INSERT INTO bookings(conversation_id,patient_id,order_ids,site_id,slot_at,confirmation_id,adapter,status,previous_booking_id,created_at,updated_at,last_error) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (conversation_id, patient_id, json.dumps(order_ids), site_id, slot_at, confirmation_id, adapter, status, previous,
                                                          now.strftime(ISO), now.strftime(ISO), error))
    log_event(conn, now.strftime(ISO), "system", "booking_%s" % status, conversation_id=conversation_id, patient_id=patient_id,
              detail={"booking_id": cur.lastrowid, "site": site_id, "slot_at": slot_at, "confirmation_id": confirmation_id, "adapter": adapter, "simulated": adapter == "simulated"})
    return cur.lastrowid


def set_status(conn, now: datetime, booking_id: int, status: str, error: Optional[str] = None) -> None:
    assert status in STATUSES
    conn.execute("UPDATE bookings SET status=?, updated_at=?, last_error=COALESCE(?, last_error) WHERE id=?", (status, now.strftime(ISO), error, booking_id))
    b = row(conn, "SELECT * FROM bookings WHERE id=?", (booking_id,))
    log_event(conn, now.strftime(ISO), "system", "booking_%s" % status, conversation_id=b["conversation_id"], patient_id=b["patient_id"],
              detail={"booking_id": booking_id, "site": b["site_id"], "slot_at": b["slot_at"], "confirmation_id": b["confirmation_id"]})


def funnel(conn) -> Dict:
    """Intention / booking / attendance / verified completion, counted separately."""
    ensure_schema(conn)
    plans = row(conn, "SELECT COUNT(*) n FROM conversations WHERE agreed_date IS NOT NULL")["n"]
    planned_ever = row(conn, "SELECT COUNT(DISTINCT conversation_id) n FROM events WHERE kind='conversation_state' AND detail LIKE '%plan_agreed%'")["n"]
    booked = row(conn, "SELECT COUNT(DISTINCT conversation_id) n FROM bookings WHERE status IN ('booked','rescheduled','attended')")["n"]
    attended = row(conn, "SELECT COUNT(DISTINCT conversation_id) n FROM bookings WHERE status='attended'")["n"]
    verified = row(conn, "SELECT COUNT(DISTINCT patient_id) n FROM orders WHERE state='verified_complete'")["n"]
    failed = row(conn, "SELECT COUNT(*) n FROM bookings WHERE status='failed'")["n"]
    cancelled = row(conn, "SELECT COUNT(*) n FROM bookings WHERE status='cancelled'")["n"]
    return {"walk_in_plans_now": plans, "conversations_ever_planned": planned_ever, "conversations_with_a_confirmed_booking": booked,
            "conversations_attended": attended, "patients_verified_complete": verified, "bookings_failed": failed, "bookings_cancelled": cancelled,
            "note": "a walk-in plan is an intention, not an appointment; attendance comes from a partner event; completion only from a partner result"}
