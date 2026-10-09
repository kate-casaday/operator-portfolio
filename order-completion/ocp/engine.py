"""Orchestrator (v0.2, after Codex first review).

Invariants enforced here, not in prompts:
  * every order state change passes rules.check_transition;
  * `verified_complete` is reachable only from a partner result event that covers every outstanding line;
  * every outbound passes through the outbox with a dedupe key, a kind (compliance/safety/reply/scheduled)
    and the conversation epoch it was queued under; stale-epoch messages are cancelled at flush;
  * a conversation in a held state (waiting_partner / escalated / paused) is human-owned: ordinary replies
    are recorded and annotated, never acted on, until the escalation is resolved;
  * the send protocol is commit-before-call: a message is marked `sending` and committed before the
    provider is called, so a crash after provider acceptance leaves an `ambiguous` row that is never
    resent automatically;
  * contact authorization (partner consent AND no local opt-out AND number not suppressed) is checked at
    enqueue and again at send; a shared phone number quarantines every patient on it;
  * every decision is written to `events`.
The model is consulted to classify intent and (version 3) to WRITE the words of a reply within the facts
the application selected; `fact_check` refuses any text that steps outside them and the approved template
is sent instead.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import importer, templates
from .db import get_setting, set_setting, log_event, row, rows
from .catalog import Catalog
from .directory import Directory
from .llm.base import ModelAdapter, ProviderError
from .llm.composer import Composer, FactComposer, ComposeRequest, fact_check, ACTION_GUIDE
from .messaging.base import MessagingAdapter, MessagingError, AmbiguousSendError
from .metrics import sms_segments
from .models import (CLINICIAN_QUEUE_REASONS, DEFAULT_HUMAN_MINUTES, ESCALATION_REASONS, HELD_STATES,
                     COMPLIANCE_KINDS, SAFETY_KINDS, MAX_HELP_REPLIES_PER_CONVERSATION, PREFERENCE_KEYS,
                     HOLD_REASONS_CLINICAL, CLINICAL_HANDOFF_MODES)
from .rules import (Policy, RuleViolation, check_transition, clinician_available, in_quiet_hours,
                    next_send_window, order_is_eligible, prescreen_inbound, usage_exceeded,
                    validate_model_output, model_attempts_allowed, add_business_hours,
                    OVERDUE_THRESHOLD_MIN, OVERDUE_THRESHOLD_MAX, time_bounds_contradict)
from .geo import LocalGeocoder, Geocoder
from .portal import PortalAdapter, SimulatedPortal, PortalError
from .llm.resolver import (Resolver, Reviewer, RulesResolver, RulesReviewer, ResolveCase, Resolution, Verdict, ESCALATE)
from . import facts as _facts, referrals as _refs, scheduling as _sched, improve as _improve, feed_integrity as _feed
from .feed_integrity import TechContactAdapter, SimulatedTechContact, FeedSource
from .scheduling import SimulatedScheduler, SchedulingError
from . import templates as _templates
import hashlib as _hashlib
import os as _os
import re as _re

def _load_prices() -> Dict:
    try:
        with open(_os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "costs", "prices.json")) as f:
            return json.load(f).get("models", {})
    except Exception:  # noqa: BLE001
        return {}

PRICES = _load_prices()

# Templates whose words the composer may rewrite.  Compliance confirmations and the clinical/staff safety
# acknowledgements (clinic phone, 911 language) are always sent exactly as approved.
COMPOSABLE_TEMPLATES = {
    "outreach_initial", "outreach_followup", "outreach_followup_reduced",
    "offer_sites", "offer_sites_constrained", "offer_sites_nearby", "no_site_matches", "no_capable_site", "ask_day", "plan_confirmed",
    "reminder", "clarify_times", "reschedule_ack", "already_completed_ack", "completed_in_network_ack",
    "completed_out_of_network_ack", "cost_ack", "transport_ack", "hold_ack", "human_ack", "fewer_reminders_ack",
    "unclear", "handoff_generic",
    # version 4: the resolver's replies and the location ask (portal / emergency / prep / menu texts are approved wording only)
    "offer_alternative", "offer_mobile_stop", "transport_resolved", "out_of_area_ack", "location_link_offer",
    # version 5: the booking offer is composable (slot times are facts); confirmations, rationale, plan-change and relay texts are approved wording only
    "offer_slots", "offer_slot_one",
}
RESOLVABLE_KINDS = ("schedule", "transport", "out_of_area")
STOP_FOOTER_TEMPLATES = {k for k, g in ACTION_GUIDE.items() if g.get("stop")}

_IN_NET_RE = _re.compile(r"\b(riverbend|brunswick lab|bath lab|your lab|yours)\b", _re.IGNORECASE)
_CLINICAL_KW_RE = _re.compile(r"\b(nurse|doctor|dr\.?|clinician|clinical|care team|provider|prescri\w*|medication|meds|results?|symptom\w*|diagnos\w*)\b", _re.IGNORECASE)
_HUMAN_KW_RE = _re.compile(r"\b(real person|a human|a person|someone|somebody|coordinator|talk to (a |some)?(one|body|person))\b", _re.IGNORECASE)
_NEG_NET_RE = _re.compile(r"\bnot (at|in) (your|the|a) (lab|riverbend|brunswick|bath)|\bnot (riverbend|brunswick|bath|yours)\b|wasn'?t (at )?(your|riverbend)", _re.IGNORECASE)
_OUT_NET_RE = _re.compile(r"\b(quest|labcorp|hospital|urgent care|somewhere else|elsewhere|another|other lab|doctor'?s office|"
                          r"walgreens|cvs|out of state|different)\b", _re.IGNORECASE)

ISO = "%Y-%m-%dT%H:%M:%S"
WEEKDAY_NAME = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
                "sat": "Saturday", "sun": "Sunday"}
OFFER_TEMPLATES = templates.offer_templates()      # derived from the registry (Sept 23, Codex V4): every site / slot / plan / booking text
SUPPRESSION_CLOSE_REASONS = {"opt_out", "wrong_number"}


class Engine:
    def __init__(self, conn: sqlite3.Connection, directory: Directory, model: ModelAdapter,
                 messaging: MessagingAdapter, policy: Optional[Policy] = None,
                 composer: Optional[Composer] = None, catalog: Optional[Catalog] = None,
                 portal: Optional[PortalAdapter] = None, resolver: Optional[Resolver] = None,
                 reviewer: Optional[Reviewer] = None, geocoder: Optional[Geocoder] = None, scheduler=None,
                 tech_contact: Optional[TechContactAdapter] = None, feed_source: Optional[FeedSource] = None):
        self.conn = conn
        self.directory = directory
        self.model = model
        self.messaging = messaging
        self.policy = policy or Policy()
        self.composer = composer if composer is not None else FactComposer()
        self.catalog = catalog or Catalog.load()
        # version 4
        self.portal = portal if portal is not None else SimulatedPortal()
        self.resolver = resolver if resolver is not None else RulesResolver()
        self.reviewer = reviewer if reviewer is not None else RulesReviewer()
        self.geocoder = geocoder if geocoder is not None else LocalGeocoder(directory.towns)
        if self.policy.clinical_handoff not in CLINICAL_HANDOFF_MODES:
            raise RuleViolation("clinical_handoff must be one of %s" % (CLINICAL_HANDOFF_MODES,))
        # version 5
        self.scheduler = scheduler if scheduler is not None else SimulatedScheduler(directory)
        # Sept 23: feed integrity — the arrival path, the partner technical-contact notifier, the pickup source
        self.tech_contact = tech_contact if tech_contact is not None else SimulatedTechContact()
        self.feed_source = feed_source
        for m in (_facts, _refs, _sched, _improve, _feed):
            m.ensure_schema(conn)

    # ------------------------------------------------------------------ clock
    def now(self) -> datetime:
        v = get_setting(self.conn, "sim_now")
        return datetime.fromisoformat(v) if v else datetime.now().replace(microsecond=0)

    def set_now(self, dt: datetime, actor: str = "system") -> None:
        set_setting(self.conn, "sim_now", dt.strftime(ISO))
        log_event(self.conn, dt.strftime(ISO), actor, "clock_set", detail={"sim_now": dt.strftime(ISO)})

    def advance(self, hours: float = 0, days: float = 0, actor: str = "kate") -> datetime:
        dt = self.now() + timedelta(hours=hours, days=days)
        self.set_now(dt, actor)
        return dt

    # ---------------------------------------------------------- global controls
    def paused(self) -> Optional[str]:
        return get_setting(self.conn, "pause_reason")

    def pause(self, reason: str, actor: str = "kate") -> None:
        set_setting(self.conn, "pause_reason", "%s:%s" % (actor, reason))
        log_event(self.conn, self.now().strftime(ISO), actor, "paused", detail={"reason": reason})

    def resume(self, actor: str = "kate") -> None:
        self.conn.execute("DELETE FROM settings WHERE key='pause_reason'")
        self.conn.commit()
        log_event(self.conn, self.now().strftime(ISO), actor, "resumed")

    # ---------------------------------------------------------- fresh synthetic session (C1)
    def seed_session(self, orders_feed: Dict, start: datetime, actor: str = "kate") -> Dict:
        """Reset the synthetic cohort and leave every eligible patient interactive (initial outreach sent).
        Feedback rows survive: their evidence JSON is self-contained, and they are also archived to a file."""
        import os
        kept = rows(self.conn, "SELECT * FROM feedback")
        if kept:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            os.makedirs(os.path.join(root, "feedback"), exist_ok=True)
            with open(os.path.join(root, "feedback", "archive-%s.json" % self.now().strftime("%Y%m%dT%H%M%S")), "w") as f:
                json.dump(kept, f, indent=1, default=str)
        self.conn.execute("UPDATE feedback SET archived=1")
        for t in ("messages", "events", "escalations", "preferences", "human_time", "model_calls", "feed_events", "feed_imports",
                  "portal_messages", "location_links", "patient_locations",          # version 4 tables first (foreign keys)
                  "referral_events", "referrals", "bookings", "fact_reviews", "fact_cards", "clinical_notes", "order_events",   # version 5
                  "conversations", "order_lines", "orders", "patients", "suppressed_numbers"):
            self.conn.execute("DELETE FROM %s" % t)
        self.conn.execute("DELETE FROM settings WHERE key IN ('pause_reason')")
        self.conn.commit()
        self.set_now(start, actor)
        r = self.import_orders(orders_feed, source_name="seed")
        self.import_updates({"partner_id": orders_feed["partner_id"], "generated_at": start.isoformat(), "updates": []},
                            source_name="seed (initial results file, empty)")
        t = self.tick()
        interactive = row(self.conn, "SELECT COUNT(*) n FROM conversations WHERE state IN ('outreach_sent','engaged','new')")["n"]
        log_event(self.conn, self.now().strftime(ISO), actor, "session_seeded",
                  detail={"patients": r.get("patients_created"), "eligible": r.get("eligible"), "interactive_conversations": interactive,
                          "feedback_rows_kept": len(kept)})
        self.conn.commit()
        return {"patients": r.get("patients_created"), "eligible": r.get("eligible"), "interactive_conversations": interactive,
                "feedback_rows_kept": len(kept), "first_tick": len(t["actions"])}

    # ---------------------------------------------------------- configuration
    def set_overdue_threshold(self, days: int, actor: str = "kate") -> int:
        """Overdue threshold in days, configurable within 15..45.  Takes effect for orders screened after the change."""
        days = int(days)
        if not (OVERDUE_THRESHOLD_MIN <= days <= OVERDUE_THRESHOLD_MAX):
            raise ValueError("overdue threshold must be between %d and %d days" % (OVERDUE_THRESHOLD_MIN, OVERDUE_THRESHOLD_MAX))
        self.policy.min_order_age_days = days
        set_setting(self.conn, "overdue_threshold_days", str(days))
        log_event(self.conn, self.now().strftime(ISO), actor, "threshold_set", detail={"overdue_threshold_days": days})
        return days

    def rescreen(self) -> Dict:
        """Re-evaluate 'ineligible' orders against the current threshold (never touches terminal/suppressed orders)."""
        now = self.now()
        changed = 0
        for o in rows(self.conn, "SELECT * FROM orders WHERE state='ineligible'"):
            p = row(self.conn, "SELECT * FROM patients WHERE id=?", (o["patient_id"],))
            ok, why = order_is_eligible(o, p, now, self.policy)
            if ok and not p["phone_ambiguous"] and not p["local_opt_out"] and not self._number_suppressed(p["phone"]):
                self._set_order_state(o, "eligible", actor="system", detail={"reason": "rescreened: " + why})
                self._ensure_conversation(p["id"], for_new_order=True)
                changed += 1
        self.conn.commit()
        return {"newly_eligible": changed}

    # ---------------------------------------------------------- preferences
    def active_preferences(self, patient_id: int) -> Dict:
        out = {}
        for r in rows(self.conn, "SELECT key, value FROM preferences WHERE patient_id=? AND superseded_by IS NULL ORDER BY id", (patient_id,)):
            v = r["value"]
            out[r["key"]] = {"true": True, "false": False}.get(v, v)
        return out

    def _record_preferences(self, patient_id: int, msg_id: Optional[int], constraints: Dict, source: str = "model_interpretation",
                            corrects: bool = False) -> List[Dict]:
        """Persist explicitly stated constraints as preferences with their source and the message they came
        from.  A different value for an existing key supersedes it and is marked corrected."""
        now = self.now().strftime(ISO)
        recorded = []
        for k in PREFERENCE_KEYS:
            if k not in constraints or constraints[k] in (None, ""):
                continue
            val = str(constraints[k]).lower() if isinstance(constraints[k], bool) else str(constraints[k])
            cur = row(self.conn, "SELECT id, value FROM preferences WHERE patient_id=? AND key=? AND superseded_by IS NULL", (patient_id, k))
            if cur and cur["value"] == val:
                continue
            corrected = 1 if (cur is not None or corrects) else 0
            c = self.conn.execute("INSERT INTO preferences(patient_id,key,value,source,message_id,corrected,created_at) VALUES(?,?,?,?,?,?,?)",
                                  (patient_id, k, val, source, msg_id, corrected, now))
            if cur:
                self.conn.execute("UPDATE preferences SET superseded_by=? WHERE id=?", (c.lastrowid, cur["id"]))
            recorded.append({"key": k, "value": val, "corrected": bool(corrected), "previous": cur["value"] if cur else None})
        # A new time bound that contradicts the active bounds replaces them: the latest statement wins.
        # before_time withdraws an active after_time AND evening_ok (evening implies after 17:00);
        # after_time / evening_ok withdraws an active before_time when the window would be empty.
        keys_now = {r["key"] for r in recorded}
        active = self.active_preferences(patient_id)
        to_withdraw = []
        if "before_time" in keys_now and "after_time" not in keys_now:
            eff_after = active.get("after_time") or ("17:00" if active.get("evening_ok") else None)
            if eff_after and eff_after >= active["before_time"]:
                to_withdraw += ["after_time", "evening_ok"]
        if ("after_time" in keys_now or "evening_ok" in keys_now) and "before_time" not in keys_now and active.get("before_time"):
            eff_after = active.get("after_time") or ("17:00" if active.get("evening_ok") else None)
            if eff_after and eff_after >= active["before_time"]:
                to_withdraw.append("before_time")
        for old_key in to_withdraw:
            old = row(self.conn, "SELECT id FROM preferences WHERE patient_id=? AND key=? AND superseded_by IS NULL", (patient_id, old_key))
            if old:
                self.conn.execute("UPDATE preferences SET superseded_by=-1 WHERE id=?", (old["id"],))   # -1 = withdrawn by contradiction
                recorded.append({"key": old_key, "value": None, "corrected": True, "withdrawn": True, "previous": active.get(old_key)})
        if recorded:
            log_event(self.conn, now, "system", "preferences_recorded", patient_id=patient_id,
                      detail={"message_id": msg_id, "source": source, "recorded": recorded})
        return recorded

    def set_preference(self, patient_id: int, key: str, value, actor: str = "kate") -> Dict:
        if key not in PREFERENCE_KEYS:
            raise ValueError("unknown preference key %r" % key)
        rec = self._record_preferences(patient_id, None, {key: value}, source=actor, corrects=True)
        self.conn.commit()
        return {"recorded": rec}

    def feed_is_stale(self, partner_id: Optional[str] = None) -> bool:
        """Per partner.  Both feed kinds (orders + updates) must be fresh: a partner that sent orders but no
        result/cancellation file cannot tell us an order is still open."""
        partner_id = partner_id or self.directory.partner_id
        now = self.now()
        for kind in ("orders", "updates"):
            latest = row(self.conn, "SELECT MAX(generated_at) g FROM feed_imports WHERE partner_id=? AND feed_kind=?",
                         (partner_id, kind))
            if not latest or not latest["g"]:
                return True
            if now - datetime.fromisoformat(latest["g"]) > timedelta(hours=self.policy.stale_feed_hours):
                return True
        return False

    # ------------------------------------------------------------------ feed integrity (Sept 23)
    def _feed_gate(self, patient: Dict, kind: str, template_id: Optional[str]) -> Optional[str]:
        """The one send-boundary rule for partner data problems.  Compliance and safety messages always leave.  A stale
        feed withholds scheduled texts and offers (the order may be closed).  A partner integrity block or a per-patient
        feed hold withholds EVERY other message — scheduled, offers, slot offers, booking confirmations, plain replies —
        except `hold_ack`, which exists to tell a patient who wrote to us that we will come back to them."""
        if kind in SAFETY_KINDS:
            return None
        pid = patient["partner_id"]
        if (kind == "scheduled" or template_id in OFFER_TEMPLATES) and self.feed_is_stale(pid):
            return "stale_feed"
        if template_id == "hold_ack":
            return None
        if self.feed_integrity_blocked(pid):
            return "feed_integrity_blocked"
        held = _feed.patient_held(self.conn, patient)
        if held:
            return "patient_held:%s" % held
        return None

    def receive_feed(self, payload, source: str = "push", source_name: str = "") -> Dict:
        """The arrival path for partner files: validate, quarantine bad rows, import the rest, receipt, health, alerts."""
        return _feed.receive(self, payload, source=source, source_name=source_name)

    def feed_integrity_blocked(self, partner_id: Optional[str] = None) -> bool:
        return _feed.blocked(self, partner_id) is not None

    # ------------------------------------------------------------------ import
    def import_validated(self, cleaned: Dict, source_name: str = "", before_commit=None) -> Dict:
        """The arrival path's import: orders and/or updates from one validated payload land in ONE transaction, or none of
        it does (Codex findings 1 and 2).  `before_commit(result)` runs inside the same transaction (per-patient holds, V1),
        so anything that can change the reported outcome is committed or rolled back with the import.  Schema creation never
        happens in here (db.ensure_tables)."""
        result: Dict = {}
        try:
            if cleaned.get("orders") is not None:
                r = importer.import_orders(self.conn, {k: v for k, v in cleaned.items() if k not in ("updates", "order_events")}, self.now(), source_name)
                r.update(self.screen_orders()); result["orders"] = r
            if cleaned.get("updates") is not None:
                result["updates"] = {"applied": self._import_updates_inner({k: v for k, v in cleaned.items() if k != "orders"}, source_name)}
            if before_commit is not None:
                before_commit(result)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return result

    def import_orders(self, feed: Dict, source_name: str = "", _validated: bool = False) -> Dict:
        """Trusted local path (seed, scenarios, tests) unless `_validated` says the arrival path already checked the file.
        Every consumed file leaves a receipt and keeps feed health current (Sept 23)."""
        try:
            result = importer.import_orders(self.conn, feed, self.now(), source_name)
            result.update(self.screen_orders())
            if not _validated:
                _feed.record_internal(self, feed, "orders", source_name, result, None)
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            if not _validated:
                try:
                    _feed.record_internal(self, feed if isinstance(feed, dict) else {}, "orders", source_name, None, str(e)); self.conn.commit()
                except Exception:  # noqa: BLE001
                    pass
            raise
        return result

    def import_updates(self, feed: Dict, source_name: str = "", _validated: bool = False) -> Dict:
        """Line facts and derived order state land in one transaction."""
        try:
            applied = self._import_updates_inner(feed, source_name)
            if not _validated:
                _feed.record_internal(self, feed, "updates", source_name, {"applied": applied}, None)
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            if not _validated:
                try:
                    _feed.record_internal(self, feed if isinstance(feed, dict) else {}, "updates", source_name, None, str(e)); self.conn.commit()
                except Exception:  # noqa: BLE001
                    pass
            raise
        return {"applied": applied}

    def _import_updates_inner(self, feed: Dict, source_name: str = "") -> List:
        """No commit in here; the caller owns the transaction."""
        if True:
            changes = importer.apply_updates(self.conn, feed, self.now(), source_name)
            applied = []
            touched_patients = set()
            for oid, change in changes:
                o = row(self.conn, "SELECT * FROM orders WHERE id=?", (oid,))
                touched_patients.add(o["patient_id"])
                if change in ("partial_result", "modified"):
                    applied.append((oid, change))
                    continue
                if change == "screen":
                    applied.append((oid, "screen"))
                    continue
                if change == "attended":
                    self._record_attendance(o)
                    applied.append((oid, "attended"))
                    continue
                try:
                    self._set_order_state(o, change, actor="partner_feed")
                    applied.append((oid, change))
                except RuleViolation as e:
                    log_event(self.conn, self.now().strftime(ISO), "partner_feed", "transition_rejected",
                              order_id=oid, detail={"error": str(e)})
                if change == "replaced":
                    # the replacement inherits the replaced order's standing: the patient is already in conversation about
                    # this plan, so the 45-day threshold does not restart (v5)
                    new_o = row(self.conn, "SELECT * FROM orders WHERE id=?", (o["superseded_by"],)) if o.get("superseded_by") else None
                    if new_o and new_o["state"] == "imported" and o["state"] in ("eligible", "outreach_active", "escalated", "claimed_complete"):
                        # V5-4: the replacement inherits ONLY the overdue-age exemption; every other eligibility rule is re-screened
                        p = row(self.conn, "SELECT * FROM patients WHERE id=?", (new_o["patient_id"],))
                        aged = dict(new_o, ordered_at=o["ordered_at"])
                        ok, why = order_is_eligible(aged, p, self.now(), self.policy)
                        if not ok:
                            self._set_order_state(new_o, "ineligible", actor="partner_feed", detail={"reason": why, "replaces": o["source_order_id"]})
                            continue
                        self._set_order_state(new_o, "eligible", actor="partner_feed", detail={"reason": "replacement of an active order inherits the overdue-age exemption; other rules re-screened", "replaces": o["source_order_id"]})
                        c = row(self.conn, "SELECT * FROM conversations WHERE patient_id=?", (o["patient_id"],))
                        if c and c["state"] not in ("closed", "new"):
                            self._set_order_state(row(self.conn, "SELECT * FROM orders WHERE id=?", (new_o["id"],)), "outreach_active", actor="partner_feed")
                    continue                          # do not close the conversation over the replaced order
                self._after_order_terminal(o["patient_id"])
            if any(c == "screen" for _, c in changes) and any(row(self.conn, "SELECT state FROM orders WHERE id=?", (oid,))["state"] == "imported" for oid, c in changes if c == "screen"):
                self.screen_orders()
            for pid in touched_patients:
                self._reconcile_after_provider_event(pid, [(oid, c, next((u.get("at") for u in feed.get("updates", []) if u.get("source_order_id") == (row(self.conn, "SELECT source_order_id FROM orders WHERE id=?", (oid,)) or {}).get("source_order_id")), None))
                                                           for oid, c in changes if row(self.conn, "SELECT patient_id FROM orders WHERE id=?", (oid,))["patient_id"] == pid])
        return applied

    def screen_orders(self) -> Dict:
        now = self.now()
        eligible = ineligible = 0
        for o in rows(self.conn, "SELECT * FROM orders WHERE state='imported'"):
            p = row(self.conn, "SELECT * FROM patients WHERE id=?", (o["patient_id"],))
            ok, why = order_is_eligible(o, p, now, self.policy)
            if ok and p["phone_ambiguous"]:
                ok, why = False, "phone number shared by more than one patient (quarantined)"
            if ok and (p["local_opt_out"] or self._number_suppressed(p["phone"])):
                self._set_order_state(o, "suppressed", actor="system", detail={"reason": "prior opt-out on this number"})
                ineligible += 1
                continue
            self._set_order_state(o, "eligible" if ok else "ineligible", actor="system", detail={"reason": why})
            if ok:
                eligible += 1
                self._ensure_conversation(p["id"], for_new_order=True)
            else:
                ineligible += 1
        return {"eligible": eligible, "ineligible": ineligible}

    # ------------------------------------------------------------- scheduler
    def recover_unprocessed_inbound(self) -> List[str]:
        """Inbound rows committed but never processed (process exit mid-handling) are processed now, in
        arrival order, bound to their stored conversation and original sender, with NO sends in between."""
        return self._drain_pending_inbound()

    def _drain_pending_inbound(self, exclude: Optional[int] = None) -> List[str]:
        out = []
        for m in rows(self.conn, "SELECT id FROM messages WHERE direction='inbound' AND status='received' ORDER BY id"):
            if m["id"] == exclude:
                continue
            log_event(self.conn, self.now().strftime(ISO), "system", "inbound_recovered", detail={"message_id": m["id"]})
            r = self._process_stored_inbound(m["id"])
            out.append("recovered_inbound:%d:%s" % (m["id"], r.get("intent")))
        return out

    def _blocked_numbers(self) -> set:
        """Numbers with a durable STOP / wrong-number that has not been successfully processed (received or
        error): no non-compliance send may go to them.  Defensive barrier behind the drain."""
        blocked = set()
        for m in rows(self.conn, "SELECT from_phone, body FROM messages WHERE direction='inbound' AND status IN ('received','error')"):
            if m["from_phone"] and prescreen_inbound(m["body"], self.policy).hard_intent in ("opt_out", "wrong_number"):
                blocked.add(m["from_phone"])
        return blocked

    def tick(self) -> Dict:
        now = self.now()
        actions: List[str] = []
        actions.extend(self.recover_unprocessed_inbound())
        for pm in rows(self.conn, "SELECT * FROM portal_messages WHERE status='sending'"):
            # V4-2: unknown outcome after a crash — never resend; a person reconciles with the partner
            self.conn.execute("UPDATE portal_messages SET status='ambiguous', last_error='crash between portal call and record' WHERE id=?", (pm["id"],))
            c = self._conv(pm["conversation_id"])
            if not row(self.conn, "SELECT 1 FROM escalations WHERE conversation_id=? AND reason='portal_ambiguous' AND status='open'", (c["id"],)):
                self._escalate(c, "portal_ambiguous", "Portal message #%d was in flight when the process stopped; confirm with the partner before any resend" % pm["id"], None)
            actions.append("portal_ambiguous:%d" % pm["id"])
        actions.extend(_feed.tick(self))       # Sept 23: pick up files, notice what did not arrive, integrity block → pause
        stale = self.feed_is_stale()
        pr = self.paused()
        if stale and not pr:
            set_setting(self.conn, "pause_reason", "policy:stale_feed")
            log_event(self.conn, now.strftime(ISO), "system", "paused", detail={"reason": "stale_feed"})
            actions.append("paused:stale_feed")
        elif not stale and pr == "policy:stale_feed":
            if self.feed_integrity_blocked():
                set_setting(self.conn, "pause_reason", _feed.PAUSE_REASON)      # fresh but held: no one-tick opening (Codex finding 3)
                actions.append("paused:feed_integrity")
            else:
                self.resume(actor="system")
                actions.append("resumed:feed_fresh")
        for conv in rows(self.conn, "SELECT * FROM conversations WHERE next_action_at IS NOT NULL AND next_action_at<=?",
                         (now.strftime(ISO),)):
            act = conv["next_action"]
            if act in ("initial_outreach", "followup"):
                actions.append(self._do_outreach(conv))
            elif act == "clinical_followup":
                actions.append(self._do_clinical_followup(conv))
            elif act == "reminder":
                actions.append(self._do_reminder(conv))
            elif act == "verify_deadline":
                actions.append(self._do_verify_deadline(conv))
            else:
                self._set_next(conv["id"], None, None)
        actions.extend(self._check_escalation_deadlines())
        actions.extend(_refs.tick(self))
        actions.extend(self._flush_outbox())
        self.conn.commit()
        return {"at": now.strftime(ISO), "actions": [a for a in actions if a]}

    def _do_outreach(self, conv: Dict) -> str:
        now = self.now()
        conv = self._conv(conv["id"])
        if conv["state"] not in ("new", "outreach_sent", "engaged"):
            self._set_next(conv["id"], None, None)
            return "skip:%d:state=%s" % (conv["id"], conv["state"])
        if not self._open_orders(conv["patient_id"]):
            self._close_conversation(conv, "no_open_orders")
            return "closed:%d:no_open_orders" % conv["id"]
        if self.paused():
            return "held:%d:%s" % (conv["id"], self.paused())
        prefs = self.active_preferences(conv["patient_id"])
        reduced = prefs.get("reminder_frequency") == "reduced"
        max_attempts = self.policy.reduced_max_outreach_attempts if reduced else self.policy.max_outreach_attempts
        interval = self.policy.reduced_followup_interval_days if reduced else self.policy.followup_interval_days
        exhausted = (conv["reduced_allowance"] is not None and conv["reduced_allowance"] <= 0) if reduced else (conv["outreach_attempts"] >= max_attempts)
        if exhausted:
            for o in self._open_orders(conv["patient_id"]):
                if o["state"] in ("eligible", "outreach_active"):
                    self._set_order_state(o, "unresolved", actor="system", detail={"reason": "max_attempts", "reduced_cadence": reduced})
            self._close_conversation(conv, "max_attempts")
            return "unresolved:%d:max_attempts" % conv["id"]
        attempt = conv["outreach_attempts"] + 1
        tpl = "outreach_initial" if attempt == 1 else ("outreach_followup_reduced" if reduced else "outreach_followup")
        variant = self._opener_variant(conv)
        queued = self._queue(conv, tpl, "scheduled", dedupe_key="conv%d:ep%d:%s:attempt%d" % (conv["id"], conv["episode"], tpl, attempt),
                             decision={"rule": "cadence", "attempt": attempt, "max_attempts": max_attempts, "interval_days": interval,
                                       "reduced_cadence": reduced, "reduced_allowance_before": conv["reduced_allowance"],
                                       "opener_variant": variant,
                                       "reason": "scheduled outreach attempt %d (limit %d)" % (attempt, max_attempts)})
        if attempt == 1 and queued:
            m = row(self.conn, "SELECT decision, composer, segments, body FROM messages WHERE dedupe_key=?", ("conv%d:ep%d:%s:attempt%d" % (conv["id"], conv["episode"], tpl, attempt),))
            if m:
                d = json.loads(m["decision"] or "{}")
                fs = d.get("fact_sheet") or {}
                body_low = (m["body"] or "").lower()
                # realized = what the patient can actually read in the sent text, not what the writer was given (V3-7 residual)
                named = [x for x in fs.get("sites", []) if (x.get("name") or "").lower() in body_low]
                variant.update({"sites_named": len(named), "writer": m["composer"], "segments": m["segments"],
                                "disclosure_realized": "short" if "automated" in body_low else "none",
                                "visit_date_present": bool(fs.get("visit_date_text")) and (fs.get("visit_date_text") or "").lower() in body_low,
                                "sites_intended": len(fs.get("sites", []))})
            self.conn.execute("UPDATE conversations SET opener_variant=? WHERE id=?", (json.dumps(variant, sort_keys=True), conv["id"]))
            log_event(self.conn, now.strftime(ISO), "system", "opener_variant_assigned", conversation_id=conv["id"], detail=variant)
        self.conn.execute("UPDATE conversations SET outreach_attempts=?, reduced_allowance=CASE WHEN reduced_allowance IS NULL THEN NULL ELSE reduced_allowance-1 END WHERE id=?",
                          (attempt, conv["id"]))
        for o in self._open_orders(conv["patient_id"]):
            if o["state"] == "eligible":
                self._set_order_state(o, "outreach_active", actor="system")
        if conv["state"] == "new":
            self._set_conv_state(conv, "outreach_sent")
        self._set_next(conv["id"], "followup", now + timedelta(days=interval),
                       reason="no reply yet; follow-up %d of %d in %d days" % (attempt + 1, max_attempts, interval) if attempt < max_attempts
                       else "attempt limit reached; next tick marks unresolved")
        return "%s:%d:attempt%d%s" % (tpl, conv["id"], attempt, "" if queued else ":dedupe_hit")

    def _do_reminder(self, conv: Dict) -> str:
        conv = self._conv(conv["id"])
        if conv["state"] != "plan_agreed" or not self._open_orders(conv["patient_id"]):
            self._set_next(conv["id"], None, None)
            log_event(self.conn, self.now().strftime(ISO), "system", "reminder_cancelled", conversation_id=conv["id"],
                      detail={"reason": "plan no longer active or no open orders"})
            return "reminder_cancelled:%d" % conv["id"]
        if self.paused():
            return "held:%d:%s" % (conv["id"], self.paused())
        site = self.directory.site(conv["agreed_site_id"], self.now()) or {}
        bk = _sched.active_booking(self.conn, conv["id"])
        if bk:
            self._queue(conv, "booking_reminder", "scheduled", dedupe_key="conv%d:booking_reminder:%s:e%d" % (conv["id"], bk["slot_at"], conv["epoch"]),
                        extra={"when": conv["agreed_when"], "slot_time": _short_time(bk["slot_at"][11:16]), "site_name": site.get("name", ""),
                               "site_address": site.get("address", ""), "confirmation_id": bk["confirmation_id"]}, site_ids=[conv["agreed_site_id"]],
                        decision={"rule": "booking_reminder", "booking_id": bk["id"], "adapter": bk["adapter"]})
        else:
            self._queue(conv, "reminder", "scheduled", dedupe_key="conv%d:reminder:%s:e%d" % (conv["id"], conv["agreed_date"], conv["epoch"]),
                        extra={"when": conv["agreed_when"], "site_name": site.get("name", ""), "site_address": site.get("address", "")},
                        site_ids=[conv["agreed_site_id"]])
        deadline = datetime.fromisoformat(conv["agreed_date"]) + timedelta(days=self.policy.plan_verification_grace_days)
        self._set_next(conv["id"], "verify_deadline", deadline)
        return "reminder:%d" % conv["id"]

    def _do_verify_deadline(self, conv: Dict) -> str:
        conv = self._conv(conv["id"])
        if not self._open_orders(conv["patient_id"]):
            self._close_conversation(conv, "no_open_orders")
            return "closed:%d" % conv["id"]
        if conv["state"] != "plan_agreed":
            self._set_next(conv["id"], None, None)
            return "skip:%d" % conv["id"]
        log_event(self.conn, self.now().strftime(ISO), "system", "plan_not_verified", conversation_id=conv["id"],
                  detail={"agreed_when": conv["agreed_when"], "agreed_date": conv["agreed_date"],
                          "note": "no partner result after grace period"})
        self._clear_plan(conv)
        self._set_conv_state(conv, "engaged", detail={"reason": "plan_not_verified"})
        self._set_next(conv["id"], "followup", self.now(), reason="planned visit date passed with no partner result; ask again")
        return "plan_not_verified:%d" % conv["id"]

    def _check_escalation_deadlines(self) -> List[str]:
        out = []
        now = self.now().strftime(ISO)
        # Acknowledgement means "seen", not "answered": the response deadline applies until the item is resolved.
        for e in rows(self.conn, "SELECT * FROM escalations WHERE status='open' AND overdue=0 AND due_at IS NOT NULL "
                                 "AND due_at<=?", (now,)):
            self.conn.execute("UPDATE escalations SET overdue=1 WHERE id=?", (e["id"],))
            log_event(self.conn, now, "system", "escalation_overdue", conversation_id=e["conversation_id"],
                      detail={"escalation_id": e["id"], "queue": e["queue"], "assigned_to": e["assigned_to"], "due_at": e["due_at"]})
            out.append("overdue:%d:%s" % (e["id"], e["queue"]))
        return out

    # ------------------------------------------------------------------ inbound
    def handle_inbound(self, phone: str, body: str, provider_message_id: Optional[str] = None,
                       provider: str = "simulated") -> Dict:
        now = self.now()
        pmid = provider_message_id or ("SIMIN-" + uuid.uuid4().hex[:12])
        raw = body or ""
        matches = rows(self.conn, "SELECT * FROM patients WHERE phone=?", (phone,))
        if not matches:
            log_event(self.conn, now.strftime(ISO), "patient", "inbound_unknown_number",
                      detail={"phone_suffix": phone[-4:], "chars": len(raw)})
            self.conn.commit()
            return {"handled": False, "reason": "unknown_number"}
        ps = prescreen_inbound(raw, self.policy)
        if len(matches) > 1:
            return self._inbound_ambiguous(phone, matches, ps, pmid, now)
        patient = matches[0]
        conv = row(self.conn, "SELECT * FROM conversations WHERE patient_id=?", (patient["id"],))
        if conv is None:
            # Never contacted (ineligible / quarantined).  Honor STOP at number level; otherwise record only.
            if ps.hard_intent in ("opt_out", "wrong_number"):
                self._suppress_number(phone, ps.hard_intent, [patient])
                log_event(self.conn, now.strftime(ISO), "patient", "inbound_stop_from_uncontacted_patient",
                          patient_id=patient["id"], detail={"provider_message_id": pmid})
            else:
                log_event(self.conn, now.strftime(ISO), "patient", "inbound_from_uncontacted_patient",
                          patient_id=patient["id"], detail={"provider_message_id": pmid, "chars": len(raw)})
            self.conn.commit()
            return {"handled": False, "reason": "no_conversation", "intent": ps.hard_intent}
        existing = row(self.conn, "SELECT * FROM messages WHERE provider_message_id=?", (pmid,))
        if existing and existing["status"] != "received":
            log_event(self.conn, now.strftime(ISO), "system", "inbound_duplicate_ignored", conversation_id=conv["id"],
                      detail={"provider_message_id": pmid})
            self.conn.commit()
            return {"handled": False, "reason": "duplicate", "conversation_id": conv["id"]}
        if existing:
            # Recorded but never processed (process died): recover the STORED row (its own conversation and
            # original sender), not the current lookup.  Exactly once.
            msg_id = existing["id"]
            log_event(self.conn, now.strftime(ISO), "system", "inbound_recovered", conversation_id=existing["conversation_id"],
                      detail={"message_id": msg_id, "provider_message_id": pmid})
        else:
            # Record the inbound (with its sender) and commit before any processing so a crash cannot lose or
            # double-process it.
            cur = self.conn.execute(
                "INSERT INTO messages(conversation_id,direction,kind,epoch,from_phone,body,segments,provider,provider_message_id,status,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (conv["id"], "inbound", "reply", conv["epoch"], phone, ps.text, sms_segments(raw), provider, pmid, "received",
                 now.strftime(ISO)))
            msg_id = cur.lastrowid
            self.conn.execute("UPDATE conversations SET inbound_count=inbound_count+1 WHERE id=?", (conv["id"],))
            log_event(self.conn, now.strftime(ISO), "patient", "inbound", patient_id=patient["id"],
                      conversation_id=conv["id"], detail={"message_id": msg_id, "flags": ps.flags, "chars": len(raw)})
            if "instruction_like_text" in ps.flags:
                log_event(self.conn, now.strftime(ISO), "system", "suspicious_inbound", conversation_id=conv["id"],
                          detail={"message_id": msg_id, "note": "instruction-like text treated as data only"})
            self.conn.commit()
        # Any OTHER pending inbound (e.g. a durable STOP left by a crash) is applied first, with no sends.
        self._drain_pending_inbound(exclude=msg_id)
        result = self._process_stored_inbound(msg_id)
        result["sent"] = self._flush_outbox()
        self.conn.commit()
        return result

    def _process_stored_inbound(self, msg_id: int) -> Dict:
        """Process one committed inbound row, bound to its stored conversation and sender.  No sends here."""
        m = row(self.conn, "SELECT * FROM messages WHERE id=?", (msg_id,))
        conv = self._conv(m["conversation_id"])
        patient = row(self.conn, "SELECT * FROM patients WHERE id=?", (conv["patient_id"],))
        ps = prescreen_inbound(m["body"], self.policy)
        result = {"handled": True, "conversation_id": conv["id"], "message_id": msg_id, "intent": None,
                  "template": None, "escalation": None, "simulated_model": None}
        try:
            self._process_inbound(conv, patient, ps, msg_id, result, sender_phone=m["from_phone"])
            self.conn.execute("UPDATE messages SET status='processed' WHERE id=?", (msg_id,))
            self.conn.commit()
        except Exception as e:  # noqa: BLE001 - software error must not strand the patient or the record
            self.conn.rollback()
            conv = self._conv(conv["id"])
            esc = self._escalate(conv, "processing_error", "Software error handling inbound #%d: %s: %s"
                                 % (msg_id, e.__class__.__name__, str(e)[:200]), msg_id)
            self._queue(conv, "handoff_generic", "reply", dedupe_key="conv%d:handoff:msg%d" % (conv["id"], msg_id))
            self.conn.execute("UPDATE messages SET status='error' WHERE id=?", (msg_id,))
            result.update(intent="processing_error", template="handoff_generic", escalation=esc)
            self.conn.commit()
        return result

    def _inbound_ambiguous(self, phone, matches, ps, pmid, now) -> Dict:
        ids = [m["id"] for m in matches]
        log_event(self.conn, now.strftime(ISO), "patient", "inbound_ambiguous_number",
                  detail={"phone_suffix": phone[-4:], "patient_ids": ids, "provider_message_id": pmid})
        if ps.hard_intent in ("opt_out", "wrong_number"):
            self._suppress_number(phone, ps.hard_intent, matches)
            self.conn.commit()
            return {"handled": True, "reason": "ambiguous_number", "intent": ps.hard_intent}
        # One identity item for Kate, attached to whichever conversation exists (or a quarantined one).
        conv = None
        for m in matches:
            conv = row(self.conn, "SELECT * FROM conversations WHERE patient_id=?", (m["id"],))
            if conv:
                break
        if conv is None:
            self.conn.execute("INSERT INTO conversations(patient_id,state,state_updated_at,created_at,closed_reason) "
                              "VALUES(?,?,?,?,?)", (matches[0]["id"], "escalated", now.strftime(ISO), now.strftime(ISO), None))
            conv = row(self.conn, "SELECT * FROM conversations WHERE patient_id=?", (matches[0]["id"],))
        open_id = row(self.conn, "SELECT id FROM escalations WHERE conversation_id=? AND reason='identity_uncertain' AND status='open'",
                      (conv["id"],))
        if not open_id:
            self._escalate(conv, "identity_uncertain", "Inbound from a number shared by %d patients; no automated reply" % len(ids), None)
        self.conn.commit()
        return {"handled": True, "reason": "ambiguous_number", "intent": "identity_uncertain", "conversation_id": conv["id"]}

    def _process_inbound(self, conv, patient, ps, msg_id, result, sender_phone: Optional[str] = None) -> None:
        now = self.now()
        sender_phone = sender_phone or patient["phone"]
        # 1. Hard, code-decided intents win over everything, including a Kate pause and any hold.
        if ps.hard_intent == "opt_out":
            self._suppress(conv, patient, "opt_out", "opt_out_confirm", msg_id, sender_phone)
            result.update(intent="opt_out", template="opt_out_confirm")
            return
        if ps.hard_intent == "wrong_number":
            self._suppress(conv, patient, "wrong_number", "wrong_number_confirm", msg_id, sender_phone)
            result.update(intent="wrong_number", template="wrong_number_confirm")
            return
        if ps.hard_intent == "emergency" and conv["state"] != "closed":
            # version 4: code decides, before any model, any hold, any pause.  The safety text carries 911 and the
            # nearest partner-verified urgent care; the partner gets an emergency-priority item if it wants one.
            self._emergency(conv, patient, ps, msg_id, result, decided_by="code")
            return
        if conv["state"] == "closed":
            log_event(self.conn, now.strftime(ISO), "system", "inbound_on_closed_conversation",
                      conversation_id=conv["id"], detail={"message_id": msg_id})
            result.update(intent="ignored_closed")
            return
        if ps.hard_intent == "help":
            n = row(self.conn, "SELECT COUNT(*) n FROM messages WHERE conversation_id=? AND template_id='help'", (conv["id"],))["n"]
            if n < MAX_HELP_REPLIES_PER_CONVERSATION:
                self._queue(conv, "help", "compliance", dedupe_key="conv%d:help:msg%d" % (conv["id"], msg_id))
                result.update(intent="help", template="help")
            else:
                log_event(self.conn, now.strftime(ISO), "system", "help_cap_reached", conversation_id=conv["id"])
                result.update(intent="help", template=None)
            return
        # 1b. version 4: a portal relay offer is outstanding; the patient's next text IS the message (verbatim).
        if conv.get("relay_pending"):
            self._relay_capture(conv, patient, ps, msg_id, result)
            return
        # 1c. version 4: a bare digit right after the numbered menu is decided by code.
        if ps.menu_digit and self.policy.unclear_menu and conv["state"] not in HELD_STATES:
            last_out = row(self.conn, "SELECT template_id FROM messages WHERE conversation_id=? AND direction='outbound' AND status='sent' ORDER BY id DESC LIMIT 1", (conv["id"],))
            if last_out and last_out["template_id"] == "unclear_menu":
                from .llm.base import ModelResult
                mapped = {"1": "needs_location", "2": "already_completed", "3": "clinical_question"}[ps.menu_digit]
                mr = ModelResult(intent=mapped, confidence=1.0, adapter="rule", model="menu-digit", simulated=True)
                mr.prev_prefs = self.active_preferences(patient["id"])
                log_event(self.conn, now.strftime(ISO), "system", "menu_digit_decided", conversation_id=conv["id"], detail={"digit": ps.menu_digit, "intent": mapped})
                result["intent"] = mapped
                result["template"], result["escalation"] = self._dispatch(self._conv(conv["id"]), patient, mapped, mr, msg_id)
                return
        # 1d. version 5: booking replies decided by code — a slot digit or WALK IN after an offer, MOVE with an active booking
        if conv.get("pending_slots") and conv["state"] == "plan_agreed":
            if ps.menu_digit in ("1", "2"):
                result.update(intent="book_slot", template=self._book_slot(conv, patient, int(ps.menu_digit) - 1, msg_id))
                return
            if _re.match(r"^\W*(walk[ -]?in|just come by|no booking|skip)\b", ps.text, _re.IGNORECASE):
                result.update(intent="walk_in", template=self._walk_in_instead(conv, patient, msg_id))
                return
        if conv.get("pending_slots") and not (ps.menu_digit in ("1", "2") or _re.match(r"^\W*(walk[ -]?in|just come by|no booking|skip)\b", ps.text, _re.IGNORECASE)):
            # V5-6: any other reply supersedes the slot offer; a later digit must not book a stale slot
            self.conn.execute("UPDATE conversations SET pending_slots=NULL WHERE id=?", (conv["id"],))
            log_event(self.conn, now.strftime(ISO), "system", "slot_offer_superseded", conversation_id=conv["id"], detail={"by_message": msg_id})
            conv = self._conv(conv["id"])
        if _re.match(r"^\W*move\b", ps.text, _re.IGNORECASE) and _sched.active_booking(self.conn, conv["id"]):
            result.update(intent="reschedule", template=self._cancel_booking(conv, patient, msg_id, "patient asked to move it"))
            return
        if _re.match(r"^\W*wait\b", ps.text, _re.IGNORECASE) and conv.get("pause_reason") == "relay_sent":
            self._set_next(conv["id"], "clinical_followup", now + timedelta(days=self.policy.relay_followup_days), reason="patient still waiting for the office's answer")
            log_event(self.conn, now.strftime(ISO), "system", "relay_wait", conversation_id=conv["id"])
            result.update(intent="wait", template=None)
            return
        # 2. Human-owned conversations: record and annotate; act only on a narrow allowlist decided below.
        if conv["state"] in HELD_STATES:
            self._process_held(conv, patient, ps, msg_id, result)
            return
        # 3. Usage ceiling before any model call (queued outbound counts as reserved).
        limit = usage_exceeded(conv, self.policy, self._queued_outbound(conv["id"]))
        if limit:
            # replies already queued were within budget and still go out (keep_queued); the handoff itself is
            # subject to the outbound ceiling and may be refused, which the audit trail records.
            self._model_unavailable_exit(conv, patient, ps, msg_id, "usage_limit", "Limit reached: %s" % limit, result)
            return
        # 3b. Application spend brake: real-money model calls stop for the day; a person answers.  Clinical wording
        # still reaches the clinician queue (V3-2).
        if not getattr(self.model, "simulated", True) and self.spend_cap_exceeded():
            self._model_unavailable_exit(conv, patient, ps, msg_id, "spend_cap", "Daily model-spend cap $%.2f reached (spent $%.2f in 24h)"
                                         % (self.policy.max_spend_usd_per_day, self.spend_last_24h_usd()), result)
            return
        # 4. Model classification with a shared, bounded attempt budget.
        context = self._model_context(conv, patient)
        budget = model_attempts_allowed(conv, self.policy)
        mr, last_err = None, None
        if budget > 0:
            try:
                mr = self.model.classify(context, ps.text, budget=budget)
            except ProviderError as e:
                last_err = e
            self._record_attempts(conv, getattr(self.model, "last_attempts", None) or [])
        else:
            last_err = ProviderError("model-call budget exhausted")
        if mr is None:
            reason = "usage_limit" if budget <= 0 else "provider_failure"
            self._model_unavailable_exit(conv, patient, ps, msg_id, reason, "Model unavailable: %s" % last_err, result)
            return
        out = validate_model_output({"intent": mr.intent, "confidence": mr.confidence, "barrier": mr.barrier,
                                     "constraints": mr.constraints})
        mr.intent, mr.confidence, mr.barrier, mr.constraints = out["intent"], out["confidence"], out["barrier"], out["constraints"]
        result["simulated_model"] = mr.simulated
        conv = self._conv(conv["id"])
        intent = mr.intent
        if mr.confidence < self.policy.model_confidence_floor and intent != "unclear":
            log_event(self.conn, now.strftime(ISO), "system", "low_confidence_downgraded", conversation_id=conv["id"],
                      detail={"intent": intent, "confidence": mr.confidence})
            intent = "unclear"
        result["intent"] = intent
        # Explicitly stated constraints become preferences (source: model interpretation of the patient's message).
        # Contradictory bounds inside one message are NOT persisted; the patient is asked which applies.
        to_store = dict(mr.constraints)
        if time_bounds_contradict(to_store):
            to_store.pop("after_time", None); to_store.pop("before_time", None)
        mr.prev_prefs = self.active_preferences(patient["id"])      # what the application believed BEFORE this message (V3-8)
        self._record_preferences(patient["id"], msg_id, to_store, corrects=bool(mr.constraints.get("corrects")))
        # V4-1: a constraint the patient called absolute ("Sundays are the only day") is recorded as hard and is never relaxed
        sched_now = [k for k in ("after_time", "before_time", "weekday", "weekend_ok") if k in to_store]
        if sched_now and (mr.constraints.get("absolute") or "absolute_wording" in ps.flags):
            prev_hard = set(json.loads(mr.prev_prefs.get("hard_constraints") or "[]"))
            self._record_preferences(patient["id"], msg_id, {"hard_constraints": json.dumps(sorted(prev_hard | set(sched_now)))}, source="patient_statement")
            mr.constraints["hard"] = sorted(prev_hard | set(sched_now))
        result["template"], result["escalation"] = self._dispatch(conv, patient, intent, mr, msg_id)
        # version 4 shadow planner: what the model would have done next, next to what the rules did.  Recorded only.
        if self.policy.planner_shadow:
            proposed = out.get("proposed_action") or getattr(mr, "proposed_action", None)
            if proposed:
                agree = self._planner_agrees(proposed, result["template"])
                log_event(self.conn, now.strftime(ISO), "system", "planner_shadow", conversation_id=conv["id"],
                          detail={"proposed": proposed, "chosen": result["template"], "intent": intent, "agree": agree,
                                  "disposition_agree": self._planner_disposition_agrees(proposed, result["template"]), "model": mr.model, "simulated": mr.simulated})

    def _model_unavailable_exit(self, conv, patient, ps, msg_id, reason: str, summary: str, result) -> None:
        """Every exit where the model cannot be consulted on an ACTIVE conversation (usage ceiling, spend cap,
        provider failure, exhausted attempt budget) runs the same code-only keyword screen the held path uses, so a
        clinical or staff request still reaches the clinician queue with its safety acknowledgement, a request for a
        person still opens a Kate item, and only ordinary replies get the generic handoff (V3-2)."""
        now = self.now()
        cid = conv["id"]
        dk = "conv%d:%%s:msg%d" % (cid, msg_id)
        log_event(self.conn, now.strftime(ISO), "system", "model_unavailable", conversation_id=cid,
                  detail={"reason": reason, "summary": summary, "note": "code-only keyword screen decides routing"})
        if _CLINICAL_KW_RE.search(ps.text):
            route = self.clinical_route()
            if route == "none":
                tpl, esc = self._clinical_route_missing(conv, patient, ps, msg_id, dk, "staff", {"rule": "model_unavailable→clinical_keywords", "reason": reason})
                result.update(intent=reason, template=tpl, escalation=esc, routing="clinical_keywords")
                return
            if route == "portal":
                # version 4: the clinical path is the portal; no paid call, no queue item, safety wording goes out
                tpl, esc = self._clinical_handoff(conv, patient, ps, msg_id, dk, kind="staff", topic=None,
                                                  decision={"rule": "model_unavailable→clinical_keywords", "reason": reason})
                result.update(intent=reason, template=tpl, escalation=esc, routing="clinical_keywords")
                return
            available = clinician_available(now, self.policy)
            esc = self._escalate(conv, "clinical_staff_request", "Clinical/staff wording while model unavailable (%s): %s" % (reason, ps.text[:160]),
                                 msg_id, after_hours=not available)
            self._set_conv_state(conv, "waiting_partner", detail={"reason": "clinical_staff_request", "escalation_id": esc, "model_unavailable": reason})
            self._set_next(cid, None, None, reason="held: clinical wording while model unavailable (%s); partner clinician owns next step" % reason)
            due = row(self.conn, "SELECT due_at FROM escalations WHERE id=?", (esc,))["due_at"]
            tpl = "staff_ack_business_hours" if (available and due[:10] == now.strftime("%Y-%m-%d")) else "staff_ack_after_hours"
            self._queue(self._conv(cid), tpl, "safety", dedupe_key=dk % "staff_unavailable",
                        decision={"rule": "model_unavailable→clinical_keywords", "reason": reason, "due_at": due})
            _refs.create(self, conv, "clinician_queue", "staff_request", msg_id, state="queued", delivery="simulated", escalation_id=esc)
            result.update(intent=reason, template=tpl, escalation=esc, routing="clinical_keywords")
            return
        if _HUMAN_KW_RE.search(ps.text):
            esc = self._escalate(conv, "human_request", "Request for a person while model unavailable (%s): %s" % (reason, ps.text[:160]), msg_id)
            self._queue(self._conv(cid), "human_ack", "reply", dedupe_key=dk % "human_unavailable",
                        decision={"rule": "model_unavailable→human_keywords", "reason": reason})
            result.update(intent=reason, template="human_ack", escalation=esc, routing="human_keywords")
            return
        esc = self._escalate(conv, reason, summary, msg_id, keep_queued=True)
        self._queue(conv, "handoff_generic", "reply", dedupe_key=dk % "handoff", decision={"rule": "model_unavailable→handoff", "reason": reason})
        result.update(intent=reason, template="handoff_generic", escalation=esc, routing="generic")

    def _process_held(self, conv, patient, ps, msg_id, result) -> None:
        """While a human owns the conversation, three things may still happen (C2):
          1. the answer to a pending "our lab or elsewhere?" question updates the claim (code keywords only);
          2. a clinical question or request for clinical staff opens a clinician item (once) with the safety ack;
          3. a request for a real person opens a Kate item (once) with the human ack.
        Everything else is recorded on the open item.  Scheduling holds are never lifted here."""
        now = self.now()
        cid = conv["id"]
        last_out = row(self.conn, "SELECT template_id FROM messages WHERE conversation_id=? AND direction='outbound' ORDER BY id DESC LIMIT 1", (cid,))
        pending_where = bool(last_out) and last_out["template_id"] == "already_completed_ack"
        claim = row(self.conn, "SELECT id FROM escalations WHERE conversation_id=? AND status='open' AND reason='already_completed_claim'", (cid,))
        # Classify first (bounded, and only within every usage ceiling) so a staff/clinical request is never
        # consumed as a location answer.  If the model is unavailable or the budget is spent, a CODE-ONLY keyword
        # screen decides between clinical / human / annotate; the location carve-out is then unreachable.
        mr = None
        model_unavailable = False
        limit = usage_exceeded(conv, self.policy, self._queued_outbound(cid))
        budget = model_attempts_allowed(conv, self.policy)
        capped = (not getattr(self.model, "simulated", True)) and self.spend_cap_exceeded()
        if limit or budget <= 0 or capped:
            model_unavailable = True
            log_event(self.conn, now.strftime(ISO), "system", "held_model_skipped", conversation_id=cid,
                      detail={"reason": limit or ("spend_cap" if capped else "model_call_budget"), "note": "code-only keyword screen used"})
        else:
            try:
                mr = self.model.classify(self._model_context(conv, patient), ps.text, budget=budget)
            except ProviderError:
                mr = None
                model_unavailable = True
            self._record_attempts(conv, getattr(self.model, "last_attempts", None) or [])
        intent = None
        if mr is not None:
            out = validate_model_output({"intent": mr.intent, "confidence": mr.confidence, "barrier": mr.barrier, "constraints": mr.constraints})
            intent = out["intent"] if out["confidence"] >= self.policy.model_confidence_floor else None
            mr.constraints = out["constraints"]
        elif model_unavailable:
            if _CLINICAL_KW_RE.search(ps.text):
                intent = "request_clinical_staff"
            elif _HUMAN_KW_RE.search(ps.text):
                intent = "request_human"
            log_event(self.conn, now.strftime(ISO), "system", "held_keyword_screen", conversation_id=cid,
                      detail={"intent": intent, "note": "model unavailable; conservative keyword routing"})
        dk = "conv%d:%%s:msg%d" % (cid, msg_id)
        if intent == "emergency":
            self._emergency(conv, patient, ps, msg_id, result, decided_by="model")
            return
        if intent in ("clinical_question", "request_clinical_staff") and self.clinical_route() == "none":
            tpl, esc = self._clinical_route_missing(conv, patient, ps, msg_id, dk, "staff" if intent == "request_clinical_staff" else "question",
                                                    {"rule": "clinical_during_hold", "held_state": conv["state"]})
            result.update(intent=intent, template=tpl, escalation=esc)
            return
        if intent in ("clinical_question", "request_clinical_staff") and self.clinical_route() == "portal":
            topic = (mr.constraints.get("topic") if mr is not None else None)
            tpl, esc = self._clinical_handoff(conv, patient, ps, msg_id, dk, kind="staff" if intent == "request_clinical_staff" else "question",
                                              topic=topic, held=True, decision={"rule": "clinical_during_hold", "intent": intent, "held_state": conv["state"]})
            result.update(intent=intent, template=tpl, escalation=esc)
            return
        if intent in ("clinical_question", "request_clinical_staff"):
            open_clin = row(self.conn, "SELECT id FROM escalations WHERE conversation_id=? AND status='open' AND queue='clinician'", (cid,))
            if open_clin:
                self._annotate(open_clin["id"], "Further clinical request while held: %s" % ps.text[:160])
            else:
                available = clinician_available(now, self.policy)
                reason = "clinical_staff_request" if intent == "request_clinical_staff" else "clinical_question"
                esc = self._escalate(conv, reason, "%s raised while conversation was held (%s)" % (ESCALATION_REASONS[reason], conv["state"]),
                                     msg_id, after_hours=not available)
                due = row(self.conn, "SELECT due_at FROM escalations WHERE id=?", (esc,))["due_at"]
                today = available and due[:10] == now.strftime("%Y-%m-%d")
                tpl = ("staff_ack_business_hours" if today else "staff_ack_after_hours") if intent == "request_clinical_staff" \
                    else ("clinical_ack_business_hours" if today else "clinical_ack_after_hours")
                self._queue(self._conv(cid), tpl, "safety", dedupe_key=dk % "clinical_held",
                            decision={"rule": "clinical_during_hold", "intent": intent, "held_state": conv["state"], "due_at": due})
                _refs.create(self, conv, "clinician_queue", "staff_request" if intent == "request_clinical_staff" else "clinical_question", msg_id, state="queued", delivery="simulated", escalation_id=esc)
                result.update(intent=intent, template=tpl, escalation=esc)
                return
            result.update(intent=intent, template=None)
            return
        if intent == "request_human":
            open_kate = row(self.conn, "SELECT id FROM escalations WHERE conversation_id=? AND status='open' AND queue='kate' AND reason='human_request'", (cid,))
            if open_kate:
                self._annotate(open_kate["id"], "Repeated request for a person: %s" % ps.text[:160])
            else:
                esc = self._escalate(conv, "human_request", "Patient asked for a real person while conversation was held (%s)" % conv["state"], msg_id)
                self._queue(self._conv(cid), "human_ack", "reply", dedupe_key=dk % "human_held",
                            decision={"rule": "human_request_during_hold", "held_state": conv["state"]})
                result.update(intent=intent, template="human_ack", escalation=esc)
                return
            result.update(intent=intent, template=None)
            return
        if claim and pending_where and mr is not None and intent in (None, "already_completed", "unclear", "confirm_plan", "willing") \
                and not _CLINICAL_KW_RE.search(ps.text) and not _HUMAN_KW_RE.search(ps.text) \
                and (_IN_NET_RE.search(ps.text) or _OUT_NET_RE.search(ps.text) or _NEG_NET_RE.search(ps.text)):
            negated = bool(_NEG_NET_RE.search(ps.text))
            in_net = bool(_IN_NET_RE.search(ps.text)) and not _OUT_NET_RE.search(ps.text) and not negated
            where = (mr.constraints.get("where") if mr else None) or ps.text[:40]
            self._record_claim(patient["id"], msg_id, {"in_network": in_net, "where": where})
            self._annotate(claim["id"], "Patient says completion was %s: %s" % ("at a partner lab" if in_net else "OUTSIDE the partner network", ps.text[:120]))
            tpl = "completed_in_network_ack" if in_net else "completed_out_of_network_ack"
            self._queue(conv, tpl, "reply", dedupe_key=dk % tpl,
                        decision={"rule": "claim_location_answer", "in_network": in_net, "negated": negated,
                                  "reason": "answer to the pending where-question; code keywords decide, model only screened for staff/human requests"})
            result.update(intent="already_completed", template=tpl)
            return
        self._annotate_escalation(conv, "Patient replied while held (%s): %s" % (conv["state"], ps.text[:200]))
        result.update(intent="held_for_human")

    def _dispatch(self, conv, patient, intent, mr, msg_id) -> Tuple[Optional[str], Optional[int]]:
        now = self.now()
        cid = conv["id"]
        dk = "conv%d:%%s:msg%d" % (cid, msg_id)
        if conv["state"] == "outreach_sent":
            self._set_conv_state(conv, "engaged")
            conv = self._conv(cid)
        prefs = self.active_preferences(patient["id"])
        eff = dict(prefs)
        eff.update({k: v for k, v in mr.constraints.items() if v not in (None, "")})
        eff["hard"] = sorted(set(json.loads(prefs.get("hard_constraints") or "[]")) | set(mr.constraints.get("hard") or []))
        if eff.get("evening_ok") and not eff.get("after_time"):
            eff["after_time"] = "17:00"          # derived from "evenings"; see _filter_sites for OR-with-weekend handling
            eff["after_time_derived"] = True
        town = eff.get("town") or patient.get("home_town")
        req = self._requirements(patient["id"])
        dec = {"intent": intent, "confidence": round(mr.confidence, 2), "constraints_from_message": mr.constraints,
               "preferences_active": prefs, "effective_constraints": eff, "model": mr.model, "simulated": mr.simulated,
               "service_requirements": sorted(req)}
        # A constraint that DIFFERS from what the application believed before this message, stated after the patient
        # has already seen an offer, is a CHANGE: the reply must acknowledge it first (feedback #1).  Repeating the same
        # constraint is not a change (V3-8).
        prev = getattr(mr, "prev_prefs", None) or {}
        changed_keys = [k for k in ("after_time", "before_time", "weekday", "weekend_ok", "evening_ok", "town")
                        if mr.constraints.get(k) not in (None, "") and str(mr.constraints[k]).lower() != str(prev.get(k, "")).lower()]
        if self._last_offered(cid):
            if changed_keys:
                dec["constraint_changed"] = True
                dec["changed_keys"] = changed_keys
            elif any(k in mr.constraints for k in ("after_time", "before_time", "weekday", "weekend_ok", "evening_ok", "town")):
                dec["constraint_repeated"] = True

        if intent == "emergency":
            ps2 = prescreen_inbound(row(self.conn, "SELECT body FROM messages WHERE id=?", (msg_id,))["body"], self.policy)
            if ps2.hard_intent != "emergency":
                # V5-5: the code floor (idiom-scoped) is the rule for both directions; a model "emergency" on a benign idiom
                # becomes a clinical question, not an alarm
                log_event(self.conn, now.strftime(ISO), "system", "emergency_downgraded", conversation_id=cid, detail={"note": "model said emergency; code screen (idiom-scoped) found no signal"})
                intent = "clinical_question"
                mr.constraints.setdefault("topic", "symptoms")
            else:
                result_stub: Dict = {}
                self._emergency(conv, patient, ps2, msg_id, result_stub, decided_by="model")
                return result_stub.get("template"), result_stub.get("escalation")
        # version 4: the patient told us where they are (zip / a place we can geocode) or that they are not near home
        if intent in ("needs_location", "scheduling_barrier", "willing") and (mr.constraints.get("zip") or mr.constraints.get("where") == "here"):
            handled = self._locate(conv, patient, mr, msg_id, dk, dec)
            if handled:
                return handled

        if intent in ("opt_out", "wrong_number"):
            if mr.confidence >= 0.8:
                tpl = "opt_out_confirm" if intent == "opt_out" else "wrong_number_confirm"
                sender = (row(self.conn, "SELECT from_phone FROM messages WHERE id=?", (msg_id,)) or {}).get("from_phone")
                self._suppress(conv, patient, intent, tpl, msg_id, sender)
                return tpl, None
            intent = "unclear"

        # Anything that offers a site or confirms a plan needs a fresh partner feed (the order may be closed).
        if intent in ("willing", "needs_location", "needs_hours", "scheduling_barrier", "confirm_plan") \
                and (self.feed_is_stale(patient["partner_id"]) or self.feed_integrity_blocked(patient["partner_id"])):
            self._queue(conv, "hold_ack", "reply", dedupe_key=dk % "hold")
            log_event(self.conn, now.strftime(ISO), "system", "offer_withheld_stale_feed" if self.feed_is_stale(patient["partner_id"]) else "offer_withheld_feed_blocked", conversation_id=cid,
                      detail={"intent": intent})
            self._set_next(cid, "followup", now)   # re-run outreach as soon as the feed is fresh
            return "hold_ack", None

        sched_keys = ("after_time", "before_time", "weekday", "weekend_ok")
        has_sched = any(eff.get(k) for k in sched_keys)
        if time_bounds_contradict(mr.constraints) and intent not in ("opt_out", "wrong_number", "clinical_question", "request_clinical_staff", "request_human"):
            self._queue(conv, "clarify_times", "reply", dedupe_key=dk % "clarify",
                        extra={"after": mr.constraints["after_time"], "before": mr.constraints["before_time"]},
                        decision=dict(dec, rule="contradictory_time_bounds", reason="after >= before in one message; ask, do not plan"))
            return "clarify_times", None
        # An existing plan that the newly stated constraints rule out is cleared and re-offered.
        if conv["state"] == "plan_agreed" and intent in ("scheduling_barrier", "correction", "willing", "needs_location", "needs_hours") \
                and any(k in mr.constraints for k in sched_keys):
            site = self.directory.site(conv["agreed_site_id"], now)
            wd = None
            for k, v in WEEKDAY_NAME.items():
                if (conv["agreed_when"] or "").startswith(v):
                    wd = k
            span = site["hours"].get(wd) if (site and wd) else None
            ok = bool(span) and (not eff.get("after_time") or span[1] > eff["after_time"]) and (not eff.get("before_time") or span[0] < eff["before_time"]) \
                and (not eff.get("weekday") or eff["weekday"] == wd)
            if not ok:
                self._clear_plan(conv)
                self._set_conv_state(conv, "engaged", detail={"reason": "plan no longer fits stated constraints"})
                self._set_next(cid, None, None)          # the old plan's reminder is no longer a next step
                conv = self._conv(cid)
                dec["plan_invalidated"] = True
        # A bare day named right after a sent offer is a choice of day, not a new constraint.
        if intent == "scheduling_barrier" and set(mr.constraints) <= {"weekday", "weekend_ok"} and mr.constraints.get("weekday") \
                and conv["state"] in ("engaged", "plan_agreed") and self._last_offered(cid):
            dec["rule_note"] = "scheduling_barrier with only a weekday after a sent offer → treated as confirm_plan"
            intent = "confirm_plan"

        if intent in ("willing", "needs_location", "needs_hours"):
            if has_sched:
                sites = self._filter_sites(eff, now, town, req)[:2]
                if sites:
                    return self._offer(conv, sites, "offer_sites_constrained", dk % "offer_c", dec=dict(dec, rule="offer:known_constraints"))
                resolved = self._try_resolve(conv, patient, "schedule", eff, msg_id, dk, dec)
                if resolved:
                    return resolved
            sites = self.directory.nearest(town, now, 2, requirements=req, point=None if eff.get("town") else self.patient_point(patient["id"]))
            tpl = "offer_sites_nearby" if eff.get("town") else "offer_sites"
            return self._offer(conv, sites, tpl, dk % "offer", extra={"town": town or ""}, dec=dict(dec, rule="offer:nearest", town=town))

        if intent in ("scheduling_barrier", "correction") and not (intent == "correction" and (mr.constraints.get("weekday") or mr.constraints.get("site_choice")) and self._last_offered(cid)):
            if intent == "correction" and conv["state"] == "plan_agreed":
                self._clear_plan(conv)
                self._set_conv_state(conv, "engaged", detail={"reason": "correction"})
                conv = self._conv(cid)
            c = {k: eff[k] for k in sched_keys if eff.get(k)}
            sites = self._filter_sites(eff, now, town, req)[:2] if c else []
            if sites:
                return self._offer(conv, sites, "offer_sites_constrained", dk % "offer_c", dec=dict(dec, rule="offer:filter", filter=c))
            if not c:
                return self._offer(conv, self.directory.nearest(town, now, 2, requirements=req), "offer_sites", dk % "offer", dec=dict(dec, rule="offer:nearest_no_constraints"))
            if not any(self.directory.capable(s, req) for s in self.directory._sites):
                return self._offer(conv, [], "no_capable_site", dk % "nocap", dec=dict(dec, rule="no_capable_site_with_constraints", filter=c))
            resolved = self._try_resolve(conv, patient, "schedule", eff, msg_id, dk, dec)
            if resolved:
                return resolved
            esc = self._escalate(conv, "unresolved_barrier", "Schedule constraint with no matching verified site: %s%s" % (json.dumps(c), self._resolver_note(dec)), msg_id)
            self._queue(conv, "no_site_matches", "reply", dedupe_key=dk % "nosite", decision=dict(dec, rule="no_site_matches", filter=c))
            return "no_site_matches", esc

        if intent == "correction":
            if conv["state"] == "plan_agreed":
                self._clear_plan(conv)
                self._set_conv_state(conv, "engaged", detail={"reason": "correction"})
                conv = self._conv(cid)
            return self._confirm_plan(conv, patient, mr, dk, eff=eff, dec=dict(dec, rule="correction→confirm_plan"))

        if intent in ("request_clinical_staff", "clinical_question") and self.clinical_route() == "none":
            return self._clinical_route_missing(conv, patient, prescreen_inbound(row(self.conn, "SELECT body FROM messages WHERE id=?", (msg_id,))["body"], self.policy),
                                                msg_id, dk, "staff" if intent == "request_clinical_staff" else "question", dict(dec, rule="clinical_route_missing"))
        if intent in ("request_clinical_staff", "clinical_question") and self.clinical_route() == "portal":
            return self._clinical_handoff(conv, patient, prescreen_inbound(row(self.conn, "SELECT body FROM messages WHERE id=?", (msg_id,))["body"], self.policy),
                                          msg_id, dk, kind="staff" if intent == "request_clinical_staff" else "question",
                                          topic=mr.constraints.get("topic"), decision=dict(dec, rule="clinical_handoff:%s" % self.policy.clinical_handoff))

        if intent == "request_clinical_staff":
            available = clinician_available(now, self.policy)
            esc = self._escalate(conv, "clinical_staff_request", "Patient asked to speak with clinical staff", msg_id, after_hours=not available)
            self._set_conv_state(conv, "waiting_partner", detail={"reason": "clinical_staff_request", "escalation_id": esc})
            self._set_next(cid, None, None, reason="held: patient asked for clinical staff; partner clinician owns next step")
            due = row(self.conn, "SELECT due_at FROM escalations WHERE id=?", (esc,))["due_at"]
            tpl = "staff_ack_business_hours" if (available and due[:10] == now.strftime("%Y-%m-%d")) else "staff_ack_after_hours"
            self._queue(self._conv(cid), tpl, "safety", dedupe_key=dk % "staff", decision=dict(dec, rule="clinical_staff_request", due_at=due))
            _refs.create(self, conv, "clinician_queue", "staff_request", msg_id, state="queued", delivery="simulated", escalation_id=esc)
            return tpl, esc

        if intent == "request_human":
            esc = self._escalate(conv, "human_request", "Patient asked for a real person", msg_id)
            self._queue(self._conv(cid), "human_ack", "reply", dedupe_key=dk % "human", decision=dict(dec, rule="human_request"))
            return "human_ack", esc

        if intent == "fewer_reminders":
            self._record_preferences(patient["id"], msg_id, {"reminder_frequency": "reduced"})
            # exactly one more (reduced) reminder from here; the attempt history stays monotonic (C10)
            self.conn.execute("UPDATE conversations SET reduced_allowance=1 WHERE id=?", (cid,))
            self._queue(conv, "fewer_reminders_ack", "reply", dedupe_key=dk % "fewer", decision=dict(dec, rule="fewer_reminders"))
            self._set_next(cid, "followup", now + timedelta(days=self.policy.reduced_followup_interval_days),
                           reason="patient asked for fewer reminders: one more follow-up in %d days, then stop" % self.policy.reduced_followup_interval_days)
            return "fewer_reminders_ack", None

        if intent == "transport_barrier":
            resolved = self._try_resolve(conv, patient, "transport", eff, msg_id, dk, dec)
            if resolved:
                return resolved
            instr = self.directory.instruction("transport", now)
            esc = self._escalate(conv, "unresolved_barrier", "Transport barrier reported%s" % self._resolver_note(dec), msg_id)
            self._queue(conv, "transport_ack", "reply", dedupe_key=dk % "transport",
                        extra={"transport_instruction": (" " + instr) if instr else ""}, instructions=["transport"] if instr else None,
                        decision=dict(dec, rule="transport_barrier", approved_instruction=bool(instr)))
            return "transport_ack", esc

        if intent == "cost_question":
            esc = self._escalate(conv, "cost_question", "Patient asked about cost/insurance", msg_id)
            self._queue(conv, "cost_ack", "reply", dedupe_key=dk % "cost", decision=dict(dec, rule="cost_question", reason="no price is ever quoted by text"))
            return "cost_ack", esc

        if intent == "clinical_question":
            available = clinician_available(now, self.policy)
            esc = self._escalate(conv, "clinical_question", "Clinical question for ordering team", msg_id,
                                 after_hours=not available)
            self._set_conv_state(conv, "waiting_partner", detail={"reason": "clinical_question", "escalation_id": esc})
            self._set_next(cid, None, None)
            due = row(self.conn, "SELECT due_at FROM escalations WHERE id=?", (esc,))["due_at"]
            # "today" is promised only when the computed response deadline actually falls today
            tpl = "clinical_ack_business_hours" if (available and due[:10] == now.strftime("%Y-%m-%d")) else "clinical_ack_after_hours"
            self._queue(self._conv(cid), tpl, "safety", dedupe_key=dk % "clinical", decision=dict(dec, rule="clinical_question", due_at=due))
            _refs.create(self, conv, "clinician_queue", "clinical_question", msg_id, topic=mr.constraints.get("topic"), state="queued", delivery="simulated", escalation_id=esc)
            return tpl, esc

        if intent == "already_completed":
            for o in self._open_orders(patient["id"]):
                if o["state"] in ("eligible", "outreach_active", "escalated"):
                    self._set_order_state(o, "claimed_complete", actor="system", detail={"note": "patient claim; not verified"})
            self._record_claim(patient["id"], msg_id, mr.constraints)
            in_net = mr.constraints.get("in_network")
            where = mr.constraints.get("where")
            esc = self._escalate(conv, "already_completed_claim", "Patient reports lab work already done%s%s" % (
                (" at %s" % where) if where else "", {True: " (partner lab)", False: " (OUTSIDE partner network)"}.get(in_net, " (location unknown)")), msg_id)
            self._set_conv_state(conv, "waiting_partner", detail={"reason": "already_completed_claim", "escalation_id": esc})
            self._set_next(cid, None, None, reason="held: patient-reported completion awaiting partner reconciliation")
            tpl = {True: "completed_in_network_ack", False: "completed_out_of_network_ack"}.get(in_net, "already_completed_ack")
            self._queue(self._conv(cid), tpl, "reply", dedupe_key=dk % "claimed", decision=dict(dec, rule="already_completed", in_network=in_net, where=where))
            return tpl, esc

        if intent == "confirm_plan":
            return self._confirm_plan(conv, patient, mr, dk, eff=eff, dec=dict(dec, rule="confirm_plan"))

        if intent == "plan_changed_report":
            return self._plan_change_reported(conv, patient, msg_id, dk, dec)

        if intent == "reschedule":
            if _sched.active_booking(self.conn, cid):
                return self._cancel_booking(conv, patient, msg_id, "patient rescheduled"), None
            if conv["state"] == "plan_agreed":
                self._clear_plan(conv)
                self._set_conv_state(conv, "engaged", detail={"reason": "reschedule"})
            self._set_next(cid, "followup", now + timedelta(days=self.policy.followup_interval_days),
                           reason="patient rescheduled; ask again in %d days if no plan" % self.policy.followup_interval_days)
            self._queue(self._conv(cid), "reschedule_ack", "reply", dedupe_key=dk % "resched", decision=dict(dec, rule="reschedule"))
            return "reschedule_ack", None

        if intent == "abusive_or_off_topic":
            esc = self._escalate(conv, "abusive_or_off_topic", "Abusive/off-topic message", msg_id)
            return None, esc

        recent = rows(self.conn, "SELECT template_id FROM messages WHERE conversation_id=? AND direction='outbound' "
                                 "ORDER BY id DESC LIMIT 1", (cid,))
        last_tpl = recent[0]["template_id"] if recent else None
        if last_tpl == "unclear_menu" or (last_tpl == "unclear" and not self.policy.unclear_menu):
            esc = self._escalate(conv, "model_low_confidence", "Consecutive unclassifiable replies (menu offered: %s)" % (last_tpl == "unclear_menu"), msg_id)
            self._queue(conv, "handoff_generic", "reply", dedupe_key=dk % "handoff", decision=dict(dec, rule="unclear_repeated→handoff"))
            return "handoff_generic", esc
        if last_tpl == "unclear":
            # version 4: a numbered menu before any person; digits are decided by code on the next text
            self._queue(conv, "unclear_menu", "reply", dedupe_key=dk % "menu", decision=dict(dec, rule="unclear_twice→menu"))
            return "unclear_menu", None
        self._queue(conv, "unclear", "reply", dedupe_key=dk % "unclear", decision=dict(dec, rule="unclear_once"))
        return "unclear", None

    def _record_claim(self, patient_id: int, msg_id: int, constraints: Dict) -> None:
        where = constraints.get("where")
        in_net = constraints.get("in_network")
        if where is None and in_net is None:
            return
        for o in rows(self.conn, "SELECT id FROM orders WHERE patient_id=? AND state IN ('claimed_complete','escalated')", (patient_id,)):
            self.conn.execute("UPDATE orders SET claim_location=COALESCE(?, claim_location), claim_in_network=COALESCE(?, claim_in_network) WHERE id=?",
                              (where, None if in_net is None else (1 if in_net else 0), o["id"]))
        log_event(self.conn, self.now().strftime(ISO), "system", "claim_location_recorded", patient_id=patient_id,
                  detail={"message_id": msg_id, "where": where, "in_network": in_net, "source": "model_interpretation"})

    def _filter_sites(self, eff: Dict, now, town: Optional[str], requirements=None) -> List[Dict]:
        """Directory filter honoring 'evenings OR weekends': a derived evening cutoff applies to weekdays only,
        so a weekend site that closes at noon still qualifies when the patient said weekends are fine.
        Only sites able to perform the order's required services are considered."""
        c = {k: eff[k] for k in ("after_time", "before_time", "weekday", "weekend_ok") if eff.get(k)}
        ls = self.catalog.timing(requirements or []).get("latest_start")
        if eff.get("after_time_derived") and eff.get("weekend_ok") and not eff.get("weekday"):
            weekday_part = self.directory.filter({k: v for k, v in c.items() if k != "weekend_ok"}, now, town, requirements=requirements, latest_start=ls)
            weekend_part = self.directory.filter({k: v for k, v in c.items() if k != "after_time"}, now, town, requirements=requirements, latest_start=ls)
            seen, out = set(), []
            for s in weekday_part + weekend_part:
                if s["id"] not in seen:
                    seen.add(s["id"]); out.append(s)
            return out
        return self.directory.filter(c, now, town, requirements=requirements, latest_start=ls)

    def _requirements(self, patient_id: int) -> set:
        """Collection services every outstanding line of the patient's open orders needs (from the catalog)."""
        codes = [r["test_code"] for r in rows(self.conn, "SELECT l.test_code FROM order_lines l JOIN orders o ON o.id=l.order_id "
                                                         "WHERE o.patient_id=? AND l.status='outstanding' AND o.state IN "
                                                         "('eligible','outreach_active','escalated','claimed_complete')", (patient_id,))]
        return self.catalog.requirements(codes)

    def _open_codes(self, patient_id: int) -> List[str]:
        return [r["test_code"] for r in rows(self.conn, "SELECT l.test_code FROM order_lines l JOIN orders o ON o.id=l.order_id "
                                                        "WHERE o.patient_id=? AND l.status='outstanding' AND o.state IN "
                                                        "('eligible','outreach_active','escalated','claimed_complete')", (patient_id,))]

    def _confirm_plan(self, conv, patient, mr, dk, eff: Optional[Dict] = None, dec: Optional[Dict] = None):
        """A plan is a concrete date at a verified site that is open that day AND within every stated time
        constraint.  Otherwise re-offer or ask."""
        now = self.now()
        cid = conv["id"]
        eff = eff or dict(mr.constraints)
        dec = dict(dec or {})
        town = eff.get("town") or patient.get("home_town")
        req = self._requirements(patient["id"])
        offered = self._last_offered(cid)
        pending = self.directory.site(conv["pending_site_id"], now) if conv["pending_site_id"] else None
        named_first = [x for x in self.directory.sites(now) if mr.constraints.get("town") and (x.get("town") or "").lower() == mr.constraints["town"].lower()
                       and self.directory.capable(x, req)] if mr.constraints.get("town") else []
        if not offered and not pending and not named_first:
            return self._offer(conv, self.directory.nearest(town, now, 2, requirements=req), "offer_sites", dk % "offer", dec=dict(dec, rule="confirm_without_offer→offer"))
        if not offered and not pending:
            pending = named_first[0]                  # "the Brunswick one, Thursday" with nothing offered yet: the named verified site is the choice
        choice = mr.constraints.get("site_choice")
        if pending and not choice:
            site = pending
        else:
            site = offered[1] if (choice == "2" and len(offered) > 1) else (offered[0] if offered else pending)
        if mr.constraints.get("town") and not choice:
            # the patient named a place: use the verified site in that town if there is one
            named = [x for x in self.directory.sites(now) if (x.get("town") or "").lower() == mr.constraints["town"].lower()
                     and self.directory.capable(x, req)]
            if named:
                site = named[0]
        weekday = mr.constraints.get("weekday") or eff.get("weekday")
        if time_bounds_contradict(eff):
            self._queue(conv, "clarify_times", "reply", dedupe_key=dk % "clarify_eff",
                        extra={"after": eff.get("after_time"), "before": eff.get("before_time")},
                        decision=dict(dec, rule="contradictory_effective_bounds", reason="stored bounds form an empty window; ask, do not plan"))
            return "clarify_times", None
        if not weekday:
            self.conn.execute("UPDATE conversations SET pending_site_id=? WHERE id=?", (site["id"], cid))
            self._queue(conv, "ask_day", "reply", dedupe_key=dk % "askday", extra={"site_name": site["name"]}, site_ids=[site["id"]],
                        decision=dict(dec, rule="ask_day", site=site["id"]))
            return "ask_day", None
        date = self.directory.next_date_for(weekday, now, site)
        feasible = date is not None
        if feasible:
            span = site["hours"].get(weekday)
            after = eff.get("after_time")
            if eff.get("after_time_derived") and weekday in ("sat", "sun") and eff.get("weekend_ok"):
                after = None                      # "evenings or weekends": a weekend day needs no evening hours
            if after and not (span and span[1] > after):
                feasible = False
            if eff.get("before_time") and not (span and span[0] < eff["before_time"]):
                feasible = False
        if feasible and not self.directory.capable(site, req):
            feasible = False
            dec["rejected_site_reason"] = self.directory.incapable_reason(site, req)
        if not feasible:
            c = {k: eff[k] for k in ("after_time", "before_time") if eff.get(k)}
            c["weekday"] = weekday
            alt = self.directory.filter(c, now, town, requirements=req, latest_start=self.catalog.timing(req).get("latest_start"))[:2]
            if alt:
                return self._offer(conv, alt, "offer_sites_constrained", dk % "offer_wd", dec=dict(dec, rule="plan_infeasible→re-offer", filter=c, rejected_site=site["id"]))
            resolved = self._try_resolve(conv, patient, "schedule", dict(eff, weekday=weekday), None, dk, dec)
            if resolved:
                return resolved
            esc = self._escalate(conv, "unresolved_barrier", "No verified site fits %s with %s%s" % (WEEKDAY_NAME.get(weekday, weekday), json.dumps(c), self._resolver_note(dec)), None)
            self._queue(conv, "no_site_matches", "reply", dedupe_key=dk % "nosite", decision=dict(dec, rule="plan_infeasible→no_site", filter=c))
            return "no_site_matches", esc
        when = "%s %s" % (WEEKDAY_NAME[weekday], date.strftime("%b %-d"))
        dec["plan_when"] = when
        self._bump_epoch(cid)   # cancels any queued reminder/plan text from an earlier plan
        self.conn.execute("UPDATE conversations SET agreed_site_id=?, agreed_when=?, agreed_date=?, pending_site_id=NULL WHERE id=?",
                          (site["id"], when, date.strftime(ISO), cid))
        conv = self._conv(cid)
        self._set_conv_state(conv, "plan_agreed", detail={"site": site["id"], "date": date.strftime(ISO)})
        # version 5: a bookable site gets two open times to choose from; a walk-in plan stays the fallback and is never called an appointment
        slots = self._slots_for(site, date, eff) if (self.policy.booking_enabled and self.capability("booking") and self.scheduler.bookable(site)) else []
        if self.policy.booking_enabled and self.scheduler.bookable(site) and not self.capability("booking"):
            log_event(self.conn, now.strftime(ISO), "system", "capability_denied", conversation_id=cid, detail={"capability": "booking", "action": "offer_slots", "fallback": "walk-in plan"})
        if slots:
            tpl = "offer_slots" if len(slots) >= 2 else "offer_slot_one"
            self.conn.execute("UPDATE conversations SET pending_slots=? WHERE id=?", (json.dumps({"site_id": site["id"], "when": when, "date": date.strftime(ISO), "slots": slots[:2]}), cid))
            self._queue(self._conv(cid), tpl, "reply", dedupe_key=dk % "slots", site_ids=[site["id"]],
                        extra={"when": when, "site_name": site["name"], "slot_1": _short_time(slots[0][11:16]), "slot_2": _short_time(slots[1][11:16]) if len(slots) > 1 else ""},
                        decision=dict(dec, rule=tpl, site=site["id"], date=date.strftime(ISO), slots=slots[:2], adapter=self.scheduler.name, simulated=self.scheduler.simulated,
                                      constraints_applied={k: eff.get(k) for k in ("after_time", "before_time")}))
            reminder_at = max(now, (date - timedelta(days=1)).replace(hour=17, minute=0))
            self._set_next(cid, "reminder", reminder_at, reason="plan agreed for %s (booking offered); reminder the day before" % when)
            return tpl, None
        self._queue(conv, "plan_confirmed", "reply", dedupe_key=dk % "plan", extra={"when": when, "site_name": site["name"]}, site_ids=[site["id"]],
                    decision=dict(dec, rule="plan_confirmed", site=site["id"], date=date.strftime(ISO), weekday=weekday,
                                  constraints_checked={k: eff.get(k) for k in ("after_time", "before_time")}))
        reminder_at = max(now, (date - timedelta(days=1)).replace(hour=17, minute=0))
        self._set_next(cid, "reminder", reminder_at, reason="plan agreed for %s; reminder at 17:00 the day before" % when)
        return "plan_confirmed", None

    # ---------------------------------------------------------------- helpers
    def _offer(self, conv, sites, tpl, dedupe_key, extra: Optional[Dict] = None, dec: Optional[Dict] = None):
        now = self.now()
        dec = dict(dec or {})
        req = set(dec.get("service_requirements") or self._requirements(conv["patient_id"]))
        ls = self.catalog.timing(req).get("latest_start")
        if not sites:
            # Distinguish "nothing can perform this order" (capability) from "nothing open then" (scheduling) (V3-5).
            # capability is judged over the whole directory (valid or not): an expired verification is a validity
            # problem (directory_empty), not a capability problem
            if not any(self.directory.capable(s, req) for s in self.directory._sites):
                unknown = self.catalog.unknown_codes(self._open_codes(conv["patient_id"]))
                reason = "unmapped_order" if unknown else "directory_empty"
                esc = self._escalate(conv, reason, "No verified site offers %s%s" % (self.catalog.describe(req),
                                     ("; unmapped codes: %s" % ", ".join(unknown)) if unknown else ""), None)
                self._queue(conv, "no_capable_site", "reply", dedupe_key=dedupe_key + ":nocap",
                            decision=dict(dec, rule="no_capable_site", reason=reason, unknown_codes=unknown))
                return "no_capable_site", esc
            esc = self._escalate(conv, "directory_empty", "No verified site to offer", None)
            self._queue(conv, "no_site_matches", "reply", dedupe_key=dedupe_key + ":none", decision=dict(dec, rule="directory_empty"))
            return "no_site_matches", esc
        link = self.directory.instruction("scheduling_link", now)
        ex = {"site_1": self.directory.describe(sites[0], requirements=req, latest_start=ls),
              "site_2": self.directory.describe(sites[1], requirements=req, latest_start=ls) if len(sites) > 1 else "(only one location listed)",
              "link_line": (" You can also book online: %s." % link) if link else ""}
        ex.update(extra or {})
        dec.update({"sites_offered": [x["id"] for x in sites[:2]], "link_offered": bool(link), "verified_directory": True,
                    "reason": dec.get("reason") or "sites from the verified directory matching the effective constraints"})
        self._queue(conv, tpl, "reply", dedupe_key=dedupe_key, extra=ex, site_ids=[x["id"] for x in sites[:2]],
                    instructions=["scheduling_link"] if link else None, decision=dec)
        log_event(self.conn, now.strftime(ISO), "system", "sites_offered", conversation_id=conv["id"],
                  detail={"site_ids": [x["id"] for x in sites[:2]], "verified_directory": True, "link_offered": bool(link),
                          "episode": conv["episode"], "dedupe_key": dedupe_key})
        return tpl, None

    def _last_offered(self, cid: int) -> List[Dict]:
        """Sites from the most recent offer in the CURRENT episode that the patient could actually have seen
        (its message was sent).  Offers from an earlier order episode, or still queued/cancelled, do not count."""
        conv = self._conv(cid)
        for ev in rows(self.conn, "SELECT detail FROM events WHERE conversation_id=? AND kind='sites_offered' ORDER BY id DESC LIMIT 5", (cid,)):
            d = json.loads(ev["detail"])
            if d.get("episode") != conv["episode"]:
                continue
            m = row(self.conn, "SELECT status FROM messages WHERE dedupe_key=?", (d.get("dedupe_key"),))
            if not m or m["status"] != "sent":
                continue
            return [s for s in (self.directory.site(i, self.now()) for i in d.get("site_ids", [])) if s]
        return []

    def _model_context(self, conv: Dict, patient: Dict) -> Dict:
        """Only this patient's conversation and open order test names.  No identifiers of anyone else."""
        history = rows(self.conn, "SELECT direction, body FROM messages WHERE conversation_id=? AND status NOT IN "
                                  "('suppressed','cancelled') ORDER BY id", (conv["id"],))
        tests = rows(self.conn, "SELECT l.test_name FROM order_lines l JOIN orders o ON o.id=l.order_id "
                                "WHERE o.patient_id=? AND l.status='outstanding' AND o.state IN "
                                "('eligible','outreach_active','escalated','claimed_complete')", (patient["id"],))
        plan = None
        if conv["agreed_site_id"]:
            s = self.directory.site(conv["agreed_site_id"], self.now())
            plan = "%s at %s" % (conv["agreed_when"], s["name"] if s else "?")
        now = self.now()
        return {"history": history, "open_order_tests": [t["test_name"] for t in tests], "agreed_plan": plan,
                "partner_name": self.directory.partner_name,
                "site_names": [x["name"] for x in self.directory.sites(now)],
                "known_towns": sorted({x.get("town") for x in self.directory.sites(now) if x.get("town")}),
                "preferences": self.active_preferences(patient["id"])}

    @staticmethod
    def attempt_cost_usd(a: Dict) -> float:
        """Priced at the attempt's own model from costs/prices.json; simulated attempts cost nothing."""
        if a.get("simulated"):
            return 0.0
        price = PRICES.get(a.get("model") or "") or PRICES.get("claude-opus-5") or {"input": 5.0, "output": 25.0, "cache_read": 0.5}
        return ((a.get("input_tokens") or 0) * price["input"] + (a.get("cache_read_tokens") or 0) * price.get("cache_read", 0)
                + (a.get("output_tokens") or 0) * price["output"]) / 1e6

    def spend_last_24h_usd(self) -> float:
        """Real-money model spend recorded by this application in the last 24 wall-clock hours."""
        since = (datetime.now() - timedelta(hours=24)).strftime(ISO)
        r = row(self.conn, "SELECT COALESCE(SUM(cost_usd),0) s FROM model_calls WHERE simulated=0 AND wall_at >= ?", (since,))
        return float(r["s"] or 0.0)

    def spend_cap_exceeded(self) -> bool:
        return self.spend_last_24h_usd() >= self.policy.max_spend_usd_per_day

    def _record_attempts(self, conv, attempts: List[Dict], purpose: str = "classify") -> None:
        now = self.now().strftime(ISO)
        wall = datetime.now().strftime(ISO)
        tokens_in = tokens_out = cache = 0
        for a in attempts:
            self.conn.execute("INSERT INTO model_calls(at,conversation_id,adapter,model,simulated,input_tokens,output_tokens,"
                              "cache_read_tokens,latency_ms,outcome,intent,confidence,purpose,cost_usd,wall_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (now, conv["id"], a.get("adapter", "?"), a.get("model", "?"), 1 if a.get("simulated") else 0,
                               a.get("input_tokens") or 0, a.get("output_tokens") or 0, a.get("cache_read_tokens") or 0,
                               a.get("latency_ms") or 0.0, a.get("outcome", "?"), a.get("intent"), a.get("confidence"),
                               purpose, self.attempt_cost_usd(a), wall))
            tokens_in += a.get("input_tokens") or 0
            tokens_out += a.get("output_tokens") or 0
            cache += a.get("cache_read_tokens") or 0
            log_event(self.conn, now, "model:%s" % a.get("model", "?"), "model_call", conversation_id=conv["id"],
                      detail={"outcome": a.get("outcome"), "simulated": bool(a.get("simulated")), "intent": a.get("intent"),
                              "confidence": a.get("confidence"), "error": a.get("error") or None,
                              "usage_known": a.get("input_tokens") is not None})
        # cache reads count toward the conversation's token ceiling too
        self.conn.execute("UPDATE conversations SET model_calls=model_calls+?, input_tokens=input_tokens+?, "
                          "output_tokens=output_tokens+? WHERE id=?", (len(attempts), tokens_in + cache, tokens_out, conv["id"]))

    def _conv(self, cid: int) -> Dict:
        return row(self.conn, "SELECT * FROM conversations WHERE id=?", (cid,))

    def _ensure_conversation(self, patient_id: int, for_new_order: bool = False) -> Dict:
        c = row(self.conn, "SELECT * FROM conversations WHERE patient_id=?", (patient_id,))
        now = self.now()
        if c:
            if for_new_order and c["state"] == "closed":
                if c["closed_reason"] in SUPPRESSION_CLOSE_REASONS:
                    for o in self._open_orders(patient_id):
                        self._set_order_state(o, "suppressed", actor="system", detail={"reason": "conversation closed by %s" % c["closed_reason"]})
                else:
                    self._bump_epoch(c["id"])
                    self.conn.execute("UPDATE conversations SET outreach_attempts=0, closed_reason=NULL, agreed_site_id=NULL, "
                                      "agreed_when=NULL, agreed_date=NULL, pending_site_id=NULL, reduced_allowance=NULL, episode=episode+1 WHERE id=?", (c["id"],))
                    open_items = rows(self.conn, "SELECT * FROM escalations WHERE conversation_id=? AND status='open'", (c["id"],))
                    if open_items:
                        # A human still owns this patient: reopen into the held state, not into outreach.
                        held = "waiting_partner" if all(e["reason"] in ("clinical_question", "already_completed_claim") for e in open_items) else "escalated"
                        self._set_conv_state(c, held, detail={"reason": "reopened for new eligible order while an item is open",
                                                              "open_escalations": [e["id"] for e in open_items]})
                        for e in open_items:
                            self._annotate(e["id"], "New eligible order arrived while this item is open; outreach is held until it is resolved.")
                        self._set_next(c["id"], None, None)
                    else:
                        self._set_conv_state(c, "new", detail={"reason": "reopened for new eligible order"})
                        self._set_next(c["id"], "initial_outreach", next_send_window(now, self.policy), reason="new eligible order; new outreach episode")
                    c = self._conv(c["id"])
            return c
        send_at = next_send_window(now, self.policy).strftime(ISO)
        self.conn.execute("INSERT INTO conversations(patient_id,state,state_updated_at,next_action,next_action_at,next_action_reason,"
                          "created_at) VALUES(?,?,?,?,?,?,?)", (patient_id, "new", now.strftime(ISO), "initial_outreach", send_at,
                                                               "eligible overdue order; first outreach at the next allowed send window", now.strftime(ISO)))
        c = row(self.conn, "SELECT * FROM conversations WHERE patient_id=?", (patient_id,))
        log_event(self.conn, now.strftime(ISO), "system", "conversation_created", patient_id=patient_id, conversation_id=c["id"],
                  detail={"first_outreach_at": send_at})
        return c

    def _open_orders(self, patient_id: int) -> List[Dict]:
        return rows(self.conn, "SELECT * FROM orders WHERE patient_id=? AND state IN "
                               "('eligible','outreach_active','escalated','claimed_complete')", (patient_id,))

    def _set_order_state(self, o: Dict, new: str, actor: str, detail: Optional[Dict] = None) -> None:
        check_transition(o["state"], new)
        now = self.now().strftime(ISO)
        self.conn.execute("UPDATE orders SET state=?, state_updated_at=?, verified_at=COALESCE(verified_at, ?) WHERE id=?",
                          (new, now, now if new == "verified_complete" else None, o["id"]))
        log_event(self.conn, now, actor, "order_state", patient_id=o["patient_id"], order_id=o["id"],
                  detail=dict({"from": o["state"], "to": new}, **(detail or {})))
        if new == "verified_complete":
            still_claimed = row(self.conn, "SELECT COUNT(*) n FROM orders WHERE patient_id=? AND state='claimed_complete' AND id!=?",
                                (o["patient_id"], o["id"]))["n"]
            if still_claimed == 0:
                for e in rows(self.conn, "SELECT e.* FROM escalations e JOIN conversations c ON c.id=e.conversation_id "
                                         "WHERE c.patient_id=? AND e.status='open' AND e.reason='already_completed_claim'", (o["patient_id"],)):
                    self._resolve(e["id"], "system", "verified by partner result feed (all claimed orders settled)", 0.0, "none", resume=False)

    def _set_conv_state(self, conv: Dict, new: str, detail: Optional[Dict] = None, keep_queued: bool = False) -> None:
        now = self.now().strftime(ISO)
        self.conn.execute("UPDATE conversations SET state=?, state_updated_at=? WHERE id=?", (new, now, conv["id"]))
        if (new in HELD_STATES or new == "closed") and not keep_queued:
            self._bump_epoch(conv["id"])
        log_event(self.conn, now, "system", "conversation_state", conversation_id=conv["id"], patient_id=conv["patient_id"],
                  detail=dict({"from": conv["state"], "to": new}, **(detail or {})))

    def _bump_epoch(self, cid: int) -> None:
        self.conn.execute("UPDATE conversations SET epoch=epoch+1 WHERE id=?", (cid,))

    def _clear_plan(self, conv: Dict) -> None:
        self.conn.execute("UPDATE conversations SET agreed_site_id=NULL, agreed_when=NULL, agreed_date=NULL, pending_site_id=NULL WHERE id=?", (conv["id"],))
        self._bump_epoch(conv["id"])
        n = self.conn.execute("UPDATE messages SET status='cancelled' WHERE conversation_id=? AND status='queued' AND template_id IN ('reminder','plan_confirmed')", (conv["id"],)).rowcount
        log_event(self.conn, self.now().strftime(ISO), "system", "reminder_cancelled", conversation_id=conv["id"],
                  detail={"reason": "plan cleared", "queued_messages_cancelled": n})

    def _set_next(self, cid: int, action: Optional[str], at: Optional[datetime], reason: Optional[str] = None) -> None:
        self.conn.execute("UPDATE conversations SET next_action=?, next_action_at=?, next_action_reason=? WHERE id=?",
                          (action, at.strftime(ISO) if at else None, reason or (None if action is None else reason), cid))

    def _after_order_terminal(self, patient_id: int) -> None:
        conv = row(self.conn, "SELECT * FROM conversations WHERE patient_id=?", (patient_id,))
        if conv and not self._open_orders(patient_id) and conv["state"] != "closed":
            if conv["next_action"] == "reminder":
                log_event(self.conn, self.now().strftime(ISO), "system", "reminder_cancelled", conversation_id=conv["id"],
                          detail={"reason": "orders closed by partner feed"})
            self._close_conversation(conv, "orders_terminal")

    def _close_conversation(self, conv: Dict, reason: str) -> None:
        self._set_conv_state(conv, "closed", detail={"reason": reason})
        self.conn.execute("UPDATE conversations SET closed_reason=? WHERE id=?", (reason, conv["id"]))
        self._set_next(conv["id"], None, None)
        n = self.conn.execute("UPDATE messages SET status='cancelled' WHERE conversation_id=? AND status='queued' AND kind IN ('scheduled','reply')",
                              (conv["id"],)).rowcount
        if n:
            log_event(self.conn, self.now().strftime(ISO), "system", "queued_messages_cancelled", conversation_id=conv["id"], detail={"n": n, "reason": reason})
        # Open escalations stay open for their owner; they are annotated, not silently resolved.
        for e in rows(self.conn, "SELECT id FROM escalations WHERE conversation_id=? AND status='open'", (conv["id"],)):
            self._annotate(e["id"], "Conversation closed (%s) while this item was open; owner decides." % reason)

    def _suppress(self, conv, patient, reason, tpl, msg_id, sender_phone: Optional[str] = None):
        """Suppress the SENDER's number always.  Patient-level effects apply only when the sender is still the
        patient's number of record; if the partner has since changed the number, the old number is suppressed,
        the new number is left untouched, and Kate gets an identity item instead of a guess."""
        sender_phone = sender_phone or patient["phone"]
        same_number = bool(sender_phone) and sender_phone == patient["phone"]
        # Confirmation is compliance-required: goes to the SENDER, bypasses pause and quiet hours.
        self._queue(conv, tpl, "compliance", dedupe_key="conv%d:%s:msg%d" % (conv["id"], tpl, msg_id), force=True,
                    to_phone=sender_phone)
        if same_number:
            for o in rows(self.conn, "SELECT * FROM orders WHERE patient_id=? AND state NOT IN "
                                     "('verified_complete','cancelled_by_partner','suppressed')", (patient["id"],)):
                self._set_order_state(o, "suppressed", actor="system", detail={"reason": reason})
            self._suppress_number(sender_phone, reason, [patient])
            self._close_conversation(conv, reason)
        else:
            self._suppress_number(sender_phone, reason, [])
            log_event(self.conn, self.now().strftime(ISO), "system", "suppression_for_previous_number",
                      conversation_id=conv["id"], patient_id=patient["id"],
                      detail={"reason": reason, "sender_suffix": (sender_phone or "")[-4:], "note": "patient's number of record differs; no patient-level change"})
            if conv["state"] != "closed":
                self._escalate(conv, "identity_uncertain", "%s received from the patient's PREVIOUS number after the partner changed it; "
                               "old number suppressed, current number untouched — confirm which is right" % reason, msg_id)

    def _suppress_number(self, phone: Optional[str], reason: str, patients: List[Dict]) -> None:
        now = self.now().strftime(ISO)
        if phone:
            self.conn.execute("INSERT OR IGNORE INTO suppressed_numbers(phone,reason,at) VALUES(?,?,?)", (phone, reason, now))
        for p in patients:
            if reason == "wrong_number":
                self.conn.execute("UPDATE patients SET phone=NULL, consent_sms=0 WHERE id=?", (p["id"],))
            else:
                self.conn.execute("UPDATE patients SET consent_sms=0, local_opt_out=1 WHERE id=?", (p["id"],))
            log_event(self.conn, now, "system", "suppressed", patient_id=p["id"],
                      detail={"reason": reason, "persistent": True, "number_level": bool(phone)})

    def _number_suppressed(self, phone: Optional[str]) -> bool:
        return bool(phone) and row(self.conn, "SELECT 1 FROM suppressed_numbers WHERE phone=?", (phone,)) is not None

    def _escalate(self, conv, reason, summary, msg_id, after_hours=False, keep_queued=False, priority: str = "normal") -> int:
        now = self.now()
        queue = "clinician" if reason in CLINICIAN_QUEUE_REASONS else "kate"
        if queue == "clinician":
            assigned = self.directory.clinician_contact.get("name", "partner clinician")
            due = add_business_hours(now, float(self.directory.clinician_contact.get("response_hours_business", 4)), self.policy)
        else:
            assigned, due = "kate", now + timedelta(days=1)
        cur = self.conn.execute("INSERT INTO escalations(conversation_id,reason,queue,after_hours,summary,assigned_to,due_at,opened_at,priority) "
                                "VALUES(?,?,?,?,?,?,?,?,?)", (conv["id"], reason, queue, 1 if after_hours else 0, summary, assigned,
                                                             due.strftime(ISO), now.strftime(ISO), priority))
        eid = cur.lastrowid
        if reason not in HOLD_REASONS_CLINICAL:
            self._set_conv_state(conv, "escalated", detail={"reason": reason, "escalation_id": eid}, keep_queued=keep_queued)
            self._set_next(conv["id"], None, None)
            for o in self._open_orders(conv["patient_id"]):
                if o["state"] in ("eligible", "outreach_active"):
                    self._set_order_state(o, "escalated", actor="system", detail={"reason": reason})
        log_event(self.conn, now.strftime(ISO), "system", "escalated", conversation_id=conv["id"], patient_id=conv["patient_id"],
                  detail={"escalation_id": eid, "reason": reason, "queue": queue, "after_hours": after_hours,
                          "assigned_to": assigned, "due_at": due.strftime(ISO), "label": ESCALATION_REASONS.get(reason, reason),
                          "handoff": "SIMULATED queue row; no external notification in v0.2"})
        return eid

    def _annotate(self, eid: int, note: str) -> None:
        e = row(self.conn, "SELECT summary FROM escalations WHERE id=?", (eid,))
        if e:
            self.conn.execute("UPDATE escalations SET summary=? WHERE id=?", (e["summary"] + "\n" + note, eid))

    def _annotate_escalation(self, conv, note: str) -> None:
        e = row(self.conn, "SELECT id FROM escalations WHERE conversation_id=? AND status='open' ORDER BY id DESC", (conv["id"],))
        if e:
            self._annotate(e["id"], note)
        else:
            # held with nothing open (should not happen); make it visible rather than silent
            self._escalate(conv, "model_low_confidence", "Reply arrived while conversation held with no open item: %s" % note[:120], None)
        log_event(self.conn, self.now().strftime(ISO), "system", "inbound_held_for_human", conversation_id=conv["id"])

    def acknowledge_escalation(self, eid: int, actor: str) -> Dict:
        e = row(self.conn, "SELECT * FROM escalations WHERE id=?", (eid,))
        if not e or e["status"] != "open":
            return {"ok": False}
        self.conn.execute("UPDATE escalations SET acknowledged_at=?, handoff_status='accepted' WHERE id=?", (self.now().strftime(ISO), eid))
        log_event(self.conn, self.now().strftime(ISO), actor, "escalation_acknowledged", conversation_id=e["conversation_id"],
                  detail={"escalation_id": eid})
        for r in rows(self.conn, "SELECT id FROM referrals WHERE escalation_id=? AND state IN ('queued','sent','offered')", (eid,)):
            _refs.transition(self, r["id"], "acknowledged", actor=actor, delivery="confirmed", detail={"escalation_id": eid, "note": "a person accepted the queue item; not yet a response"})
        self.conn.commit()
        return {"ok": True}

    def resolve_escalation(self, eid: int, actor: str, resolution: str, minutes: Optional[float] = None,
                           next_step: str = "resume") -> Dict:
        e = row(self.conn, "SELECT * FROM escalations WHERE id=?", (eid,))
        if not e or e["status"] != "open":
            return {"ok": False, "reason": "not open"}
        source = "logged"
        if minutes is None:
            minutes, source = DEFAULT_HUMAN_MINUTES.get(e["reason"], 3.0), "default_assumed"
        if next_step == "external_verified":
            if not (resolution or "").strip():
                return {"ok": False, "reason": "external_verified requires an evidence note (who confirmed, where, when)"}
            conv = self._conv(e["conversation_id"])
            for o in rows(self.conn, "SELECT * FROM orders WHERE patient_id=? AND state IN ('claimed_complete','escalated')", (conv["patient_id"],)):
                self._set_order_state(o, "completed_external", actor=actor, detail={"evidence_note": resolution, "escalation_id": eid,
                                                                                    "note": "human-attested completion outside the partner network; NOT a partner-verified result"})
            self._resolve(eid, actor, resolution, minutes, source, resume=False, close=False)
            self._after_order_terminal(conv["patient_id"])
            self.conn.commit()
            return {"ok": True, "minutes": minutes, "minutes_source": source, "orders": "completed_external"}
        self._resolve(eid, actor, resolution, minutes, source, resume=(next_step == "resume"), close=(next_step == "close"))
        self.conn.commit()
        return {"ok": True, "minutes": minutes, "minutes_source": source}

    def _resolve(self, eid, actor, resolution, minutes, source, resume=True, close=False):
        now = self.now().strftime(ISO)
        e = row(self.conn, "SELECT * FROM escalations WHERE id=?", (eid,))
        self.conn.execute("UPDATE escalations SET status='resolved', resolved_at=?, resolved_by=?, resolution=?, human_minutes=?, "
                          "minutes_source=? WHERE id=?", (now, actor, resolution, minutes, source, eid))
        if minutes and e["queue"] != "clinician":
            self.conn.execute("INSERT INTO human_time(at,actor,activity,minutes,source,escalation_id) VALUES(?,?,?,?,?,?)",
                              (now, actor, "resolve:%s" % e["reason"], minutes, source, eid))
        log_event(self.conn, now, actor, "escalation_resolved", conversation_id=e["conversation_id"],
                  detail={"escalation_id": eid, "resolution": resolution, "minutes": minutes, "minutes_source": source, "resume": resume})
        for r in rows(self.conn, "SELECT id, kind FROM referrals WHERE escalation_id=? AND state NOT IN ('resolved','cancelled')", (eid,)):
            if e["queue"] == "clinician" and actor not in ("system",):
                _refs.transition(self, r["id"], "responded", actor=actor, detail={"escalation_id": eid, "resolution": resolution, "note": "the clinician closed the queue item: response evidence"})
            else:
                _refs.note(self, r["id"], actor, "linked escalation #%d resolved by %s: %s" % (eid, actor, (resolution or "")[:120]))
        conv = self._conv(e["conversation_id"])
        if close and conv["state"] != "closed":
            for o in self._open_orders(conv["patient_id"]):
                self._set_order_state(o, "unresolved", actor=actor, detail={"escalation_id": eid})
            self._close_conversation(conv, "closed_by_%s" % actor)
        elif resume and conv["state"] in HELD_STATES:
            still_open = row(self.conn, "SELECT COUNT(*) n FROM escalations WHERE conversation_id=? AND status='open'", (conv["id"],))["n"]
            if still_open == 0:
                for o in self._open_orders(conv["patient_id"]):
                    if o["state"] in ("escalated", "claimed_complete"):
                        self._set_order_state(o, "outreach_active", actor=actor, detail={"resumed_after": eid})
                self._set_conv_state(conv, "engaged", detail={"resumed_after": eid})
                self._set_next(conv["id"], "followup", self.now())

    def record_link_click(self, conversation_id: int) -> Dict:
        conv = self._conv(conversation_id)
        if not conv:
            return {"ok": False}
        log_event(self.conn, self.now().strftime(ISO), "patient", "link_clicked", conversation_id=conversation_id,
                  patient_id=conv["patient_id"], detail={"note": "signal only; no state change; not completion"})
        self.conn.commit()
        return {"ok": True, "state_changed": False}

    def record_human_time(self, actor: str, activity: str, minutes: float) -> None:
        self.conn.execute("INSERT INTO human_time(at,actor,activity,minutes,source) VALUES(?,?,?,?,'logged')",
                          (self.now().strftime(ISO), actor, activity, minutes))
        self.conn.commit()

    # ---------------------------------------------------------------- outbox
    def _queued_outbound(self, cid: int) -> int:
        return row(self.conn, "SELECT COUNT(*) n FROM messages WHERE conversation_id=? AND direction='outbound' AND status IN ('queued','sending')", (cid,))["n"]

    def _contact_allowed(self, patient: Dict) -> Optional[str]:
        if not patient["phone"]:
            return "no phone"
        if patient["local_opt_out"]:
            return "local opt-out"
        if self._number_suppressed(patient["phone"]):
            return "number suppressed"
        if not patient["consent_sms"]:
            return "no partner consent"
        if patient["phone_ambiguous"]:
            return "phone shared by multiple patients"
        return None

    def _queue(self, conv: Dict, template_id: str, kind: str, dedupe_key: str, extra: Optional[Dict] = None,
               force: bool = False, site_ids: Optional[List[str]] = None, to_phone: Optional[str] = None,
               instructions: Optional[List[str]] = None, decision: Optional[Dict] = None) -> bool:
        """Insert an outbound message.  Returns False on a dedupe hit or an authorization refusal."""
        now = self.now().strftime(ISO)
        if row(self.conn, "SELECT id FROM messages WHERE dedupe_key=?", (dedupe_key,)):
            log_event(self.conn, now, "system", "duplicate_send_prevented", conversation_id=conv["id"], detail={"dedupe_key": dedupe_key})
            return False
        patient = row(self.conn, "SELECT * FROM patients WHERE id=?", (conv["patient_id"],))
        conv = self._conv(conv["id"])
        if kind not in COMPLIANCE_KINDS:
            if not self.capability("messaging"):
                log_event(self.conn, now, "system", "outbound_refused", conversation_id=conv["id"],
                          detail={"template": template_id, "reason": "capability 'messaging' not granted by the partner (V5-1)"})
                self._deny(conv, "messaging", "outbound %s" % template_id)
                return False
            why = self._contact_allowed(patient)
            if why:
                log_event(self.conn, now, "system", "outbound_refused", conversation_id=conv["id"],
                          detail={"template": template_id, "reason": why})
                return False
            if kind not in SAFETY_KINDS:
                over = conv["outbound_count"] + self._queued_outbound(conv["id"]) >= self.policy.max_outbound_per_conversation
                if over:
                    log_event(self.conn, now, "system", "outbound_refused", conversation_id=conv["id"],
                              detail={"template": template_id, "reason": "outbound ceiling"})
                    if not row(self.conn, "SELECT 1 FROM escalations WHERE conversation_id=? AND reason='usage_limit' AND status='open'", (conv["id"],)):
                        # replies already queued were within budget; they still go out (keep_queued)
                        self._escalate(conv, "usage_limit", "Outbound ceiling reached while queuing %s" % template_id, None,
                                       keep_queued=True)
                    return False
        o = row(self.conn, "SELECT ordering_provider FROM orders WHERE patient_id=? ORDER BY id LIMIT 1", (patient["id"],))
        ctx = {"partner_name": self.directory.partner_name,
               "first_name": (patient["display_name"] or "").split(" ")[0],
               "provider": (o or {}).get("ordering_provider") or "your care team",
               "clinician_name": self.directory.clinician_contact.get("name", "your care team"),
               "clinician_phone": self.directory.clinician_contact.get("phone", "your clinic"),
               "site_1": "", "site_2": "", "site_name": "", "site_address": "", "when": "", "transport_instruction": "", "link_line": "",
               "town": "", "after": "", "before": "",
               # version 4
               "portal_name": (self.directory.portal(self.now()) or {}).get("name", "the patient portal"),
               "portal_link": (self.directory.portal(self.now()) or {}).get("link", ""),
               "prep_instruction": "", "urgent_care_line": "", "asked_line": "", "alt_line": "", "stop_town": "", "stop_day": "",
               "stop_window": "", "stop_address": "", "nearest_site": "", "nearest_miles": "", "location_link": "",
               "test_category": self.catalog.category(self._open_codes(patient["id"]))}
        ctx.update(extra or {})
        body = templates.render(template_id, ctx)
        segs = sms_segments(body)
        if segs > self.policy.max_message_segments:
            raise RuleViolation("template %s renders to %d segments" % (template_id, segs))
        decision = dict(decision or {})
        composer_used = "template"
        if self.composer is not None and template_id in COMPOSABLE_TEMPLATES and kind not in COMPLIANCE_KINDS:
            body, composer_used, fs = self._compose(conv, patient, template_id, kind, body, site_ids or [], instructions or [], extra or {}, decision)
            decision["fact_sheet"] = fs
            segs = sms_segments(body)
            # Every fact the text may carry becomes a send dependency, so a directory change before send cancels it
            # and the response-time snapshot carries the approval records (V3-3).
            fs_site_ids = [s["id"] for s in fs.get("sites", [])]
            if fs_site_ids and not site_ids:
                site_ids = fs_site_ids
            if fs.get("scheduling_link") and "scheduling_link" not in (instructions or []):
                instructions = list(instructions or []) + ["scheduling_link"]
        status = "queued"
        if not force and conv["state"] == "closed":
            status = "suppressed"
        deps = {"sites": {sid: self.directory.site_meta(sid) for sid in (site_ids or [])},
                "instructions": {k: self.directory.instruction_meta(k) for k in (instructions or [])}}
        decision.setdefault("template", template_id)
        decision.setdefault("kind", kind)
        decision["composer"] = composer_used
        records = {"sites": {sid: next((x for x in self.directory._sites if x["id"] == sid), None) for sid in (site_ids or [])},
                   "instructions": {k: self.directory._instructions.get(k) for k in (instructions or [])}}
        decision["response_time_snapshot"] = {
            "sim_clock": now, "overdue_threshold_days": self.policy.min_order_age_days,
            "policy": {f: (getattr(self.policy, f).strftime("%H:%M") if hasattr(getattr(self.policy, f), "strftime") else getattr(self.policy, f))
                       for f in self.policy.__dataclass_fields__},
            "directory_records": records,
            "model_adapter": getattr(self.model, "name", "?"), "model_id": getattr(self.model, "model", "mock-rules-v1"),
            "model_simulated": bool(getattr(self.model, "simulated", True)),
            "template_text": _templates.TEMPLATES.get(template_id), "rendered_body_sha1": _hashlib.sha1(body.encode()).hexdigest()[:12],
            "directory_content": deps, "feed_stale": self.feed_is_stale(patient["partner_id"]), "feed_blocked": self.feed_integrity_blocked(patient["partner_id"]),
            "conversation_state": conv["state"], "epoch": conv["epoch"], "episode": conv["episode"],
        }
        self.conn.execute("INSERT INTO messages(conversation_id,direction,kind,epoch,to_phone,site_ids,content_deps,decision,body,template_id,segments,dedupe_key,status,created_at,composer)"
                          " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (conv["id"], "outbound", kind, conv["epoch"], to_phone or patient["phone"], json.dumps(site_ids) if site_ids else None,
                           json.dumps(deps) if (site_ids or instructions) else None, json.dumps(decision, default=str), body, template_id,
                           segs, dedupe_key, status, now, composer_used))
        log_event(self.conn, now, "system", "outbound_queued", conversation_id=conv["id"],
                  detail={"template": template_id, "kind": kind, "segments": segs, "dedupe_key": dedupe_key, "status": status, "force": force})
        return True


    # ---------------------------------------------------------------- version 3: fact sheet, composer, opener variant
    def _opener_variant(self, conv: Dict) -> Dict:
        """Which first-text variant this conversation gets.  Recorded so reply/plan/completion rates can be
        compared by variant on the Operations page.  Disclosure wording is partner-negotiated and A/B tested."""
        mode = get_setting(self.conn, "opener_disclosure", self.policy.opener_disclosure)
        if mode == "ab":
            disclosure = "short" if conv["id"] % 2 else "none"
        else:
            disclosure = mode if mode in ("none", "short") else "none"
        sites = int(get_setting(self.conn, "opener_sites", str(self.policy.opener_sites)))
        o = row(self.conn, "SELECT visit_at FROM orders WHERE patient_id=? AND state IN ('eligible','outreach_active') ORDER BY id LIMIT 1", (conv["patient_id"],))
        return {"structure": "kate-v3", "disclosure": disclosure, "sites": max(1, min(3, sites)),
                "visit_date_present": bool(o and o.get("visit_at")), "composer": getattr(self.composer, "name", "template")}

    def _fact_sheet(self, conv: Dict, patient: Dict, template_id: str, kind: str, site_ids: List[str], instructions: List[str],
                    extra: Dict, decision: Dict) -> Dict:
        """Everything the composed text may assert.  Built from the verified directory, the partner feed and the
        application's own decision; nothing else reaches the model as a fact."""
        now = self.now()
        codes = self._open_codes(patient["id"])
        req = self.catalog.requirements(codes)
        o = row(self.conn, "SELECT ordering_provider, visit_at FROM orders WHERE patient_id=? ORDER BY id LIMIT 1", (patient["id"],)) or {}
        provider = o.get("ordering_provider") or "your care team"
        office = ("%s's office" % provider) if provider.lower().startswith("dr") else provider
        eff = decision.get("effective_constraints") or self.active_preferences(patient["id"])
        wd = eff.get("weekday")
        weekend = bool(eff.get("weekend_ok")) and not wd
        variant = json.loads(conv["opener_variant"]) if conv.get("opener_variant") else (decision.get("opener_variant") or {})
        # sites: those the application selected for this message; for an opener, the nearest capable ones
        sites = []
        if site_ids:
            sites = [s for s in (self.directory.site(i, now) for i in site_ids) if s]
        elif template_id in ("outreach_initial", "outreach_followup", "outreach_followup_reduced"):
            sites = self.directory.nearest(patient.get("home_town"), now, variant.get("sites", self.policy.opener_sites), requirements=req)
        timing = self.catalog.timing(req)
        fs_sites = []
        for s in sites:
            latest = self.directory.latest_start(s, req, default=timing.get("latest_start"))   # one rule with selection (V3-5 residual)
            fs_sites.append({"id": s["id"], "name": s["name"], "town": s.get("town"), "address": s["address"], "hours": s["hours"],
                             "hours_text": self.directory.describe_hours(s),
                             "hours_relevant": self.directory.hours_for(s, wd, weekend) if (wd or weekend) else None,
                             "visit_requirements": s.get("visit_requirements"),
                             "latest_start_text": ("must start by %s" % _short_time(latest)) if latest else None})
        visit_date_text = None
        if o.get("visit_at"):
            try:
                visit_date_text = datetime.fromisoformat(o["visit_at"][:19]).strftime("%b %-d")
            except ValueError:
                visit_date_text = None
        replied = (conv.get("inbound_count") or 0) > 0
        all_names = [self.catalog.tests.get(c.upper(), {}).get("name") for c in codes]
        nameable = self.catalog.nameable(codes) if replied else []
        forbidden = [n for n in all_names if n and n not in nameable]
        link = self.directory.instruction("scheduling_link", now) if ("scheduling_link" in instructions or template_id in
                                                                     ("outreach_initial", "outreach_followup", "offer_sites", "offer_sites_constrained", "offer_sites_nearby")) else None
        prior_out = " ".join(m["body"] for m in rows(self.conn, "SELECT body FROM messages WHERE conversation_id=? AND direction='outbound' AND status='sent'", (conv["id"],)))
        patient_said = [m["body"] for m in rows(self.conn, "SELECT body FROM messages WHERE conversation_id=? AND direction='inbound' ORDER BY id DESC LIMIT 6", (conv["id"],))]
        already = []
        if any(s["address"] in prior_out for s in fs_sites):
            already.append("address")
        if any((s.get("visit_requirements") or "zzz") in prior_out for s in fs_sites):
            already.append("preparation line (visit requirements)")
        if link and link in prior_out:
            already.append("booking link")
        if any(s["hours_text"] in prior_out for s in fs_sites):
            already.append("full weekly hours")
        fs = {
            "sender": "%s:" % self.directory.partner_name, "partner_name": self.directory.partner_name,
            "facts_already_sent": already, "patient_said": patient_said,
            "patient_constraints": eff,
            "first_name": (patient.get("display_name") or "").split(" ")[0],
            "provider": provider, "provider_office": office,
            "visit_date_text": visit_date_text,
            "test_category": self.catalog.category(codes), "nameable_tests": nameable, "forbidden_test_names": forbidden,
            "service_requirements": self.catalog.describe(req), "timing": timing,
            "patient_town": patient.get("home_town"), "known_towns": sorted(set(list(self.directory.towns.keys()) + [s.get("town") for s in self.directory._sites if s.get("town")])),
            "sites": fs_sites, "scheduling_link": link,
            "clinician_name": self.directory.clinician_contact.get("name"), "clinician_phone": self.directory.clinician_contact.get("phone"),
            "transport_instruction": (extra.get("transport_instruction") or "").strip() or None,
            "plan_when": extra.get("when") or decision.get("plan_when"), "plan_site": extra.get("site_name") or None,
            "caregiver": str(eff.get("caregiver", "")).lower() == "true",
            "disclose_automation": variant.get("disclosure", "none"),
            "stop_footer_required": template_id in STOP_FOOTER_TEMPLATES,
            "constraint_changed": bool(decision.get("constraint_changed")),
            "conversation_state": conv["state"], "action": template_id,
            "action_guide": ACTION_GUIDE.get(template_id, {"must_say": ["the substance of the example"], "question": True}),
            "site_names": [s["name"] for s in fs_sites],
        }
        # version 4: distance from the patient's location (stored point, else town centroid); portal; resolver facts
        origin = self.patient_point(patient["id"]) or self.directory.town_coords(patient.get("home_town"))
        for s, src in zip(fs_sites, sites):
            mi = self.directory.miles_from(origin, src)
            s["distance_miles"] = mi
            s["distance_text"] = ("about %s miles" % (int(round(mi)) if mi >= 2 else mi)) if mi is not None else None
        rat = _facts.rationale_for_reply(self, patient["id"])
        fs["documented_rationale"] = [{"author": d["author"], "excerpt": d["excerpt"]} for d in rat["documented"]]
        portal = self.directory.portal(now)
        fs["portal_name"] = portal["name"] if portal else None
        fs["portal_link"] = portal["link"] if portal else None
        for k in ("asked_line", "alt_line", "stop_town", "stop_day", "stop_window", "stop_address", "route_name", "nearest_site", "nearest_miles", "location_link",
                  "slot_1", "slot_2", "when"):
            if extra.get(k) not in (None, ""):
                fs[k] = extra[k]
        return fs

    def _compose(self, conv: Dict, patient: Dict, template_id: str, kind: str, template_body: str, site_ids: List[str],
                 instructions: List[str], extra: Dict, decision: Dict):
        """Ask the composer for the words; fact-check; fall back to the approved template on any refusal.
        Returns (body, composer_used, fact_sheet)."""
        now = self.now()
        fs = self._fact_sheet(conv, patient, template_id, kind, site_ids, instructions, extra, decision)
        live = not getattr(self.composer, "simulated", True)
        if live and self.spend_cap_exceeded():
            log_event(self.conn, now.strftime(ISO), "system", "composer_skipped", conversation_id=conv["id"],
                      detail={"template": template_id, "reason": "spend_cap", "spent_24h_usd": round(self.spend_last_24h_usd(), 4)})
            return template_body, "template", fs
        history = rows(self.conn, "SELECT direction, body FROM messages WHERE conversation_id=? AND status NOT IN ('suppressed','cancelled') ORDER BY id", (conv["id"],))
        example = template_body
        if live:
            # the deterministic writer already follows Kate's structure for openers and changed-constraint offers;
            # give the model that as its example instead of the stiff round-one template
            probe = FactComposer().compose(ComposeRequest(action=template_id, fact_sheet=fs, template_text=template_body, thread=history,
                                                          constraints=decision.get("effective_constraints") or {}, decision=decision,
                                                          max_segments=self.policy.max_message_segments))
            if not fact_check(probe.text, fs, self.policy.max_message_segments):
                example = probe.text
        fs["example_text"] = example          # approved wording is allowed vocabulary for the checker
        req = ComposeRequest(action=template_id, fact_sheet=fs, template_text=example, thread=history,
                             constraints=decision.get("effective_constraints") or self.active_preferences(patient["id"]),
                             decision=decision, max_segments=self.policy.max_message_segments)
        attempts = 1 + (self.policy.composer_retries if live else 0)
        for i in range(attempts):
            if live and i > 0 and self.spend_cap_exceeded():
                log_event(self.conn, now.strftime(ISO), "system", "composer_skipped", conversation_id=conv["id"],
                          detail={"template": template_id, "reason": "spend_cap_before_retry", "spent_24h_usd": round(self.spend_last_24h_usd(), 4)})
                break
            try:
                res = self.composer.compose(req)
            except ProviderError as e:
                usage = getattr(e, "usage", None) or {}
                self._record_attempts(conv, [{"adapter": getattr(self.composer, "name", "?"), "model": getattr(self.composer, "model", "?"),
                                              "simulated": not live, "outcome": "error", "error": str(e),
                                              "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                                              "cache_read_tokens": usage.get("cache_read_tokens")}], purpose="compose")
                log_event(self.conn, now.strftime(ISO), "system", "composer_failed", conversation_id=conv["id"],
                          detail={"template": template_id, "error": str(e)[:200], "fallback": "approved template"})
                return template_body, "template", fs
            if res.simulated:
                # a deterministic writer is not a model call: it costs nothing and does not count toward the ceiling
                log_event(self.conn, now.strftime(ISO), "composer:%s" % res.composer, "composer_call", conversation_id=conv["id"],
                          detail={"template": template_id, "simulated": True, "latency_ms": round(res.latency_ms, 1)})
            else:
                self._record_attempts(conv, [{"adapter": res.composer, "model": res.model, "simulated": False, "outcome": "ok",
                                              "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                                              "cache_read_tokens": res.cache_read_tokens, "latency_ms": res.latency_ms}], purpose="compose")
            violations = fact_check(res.text, fs, self.policy.max_message_segments)
            if not violations:
                decision["composer_structured"] = res.structured
                decision["composer_model"] = res.model
                return res.text, res.composer, fs
            log_event(self.conn, now.strftime(ISO), "system", "composer_refused", conversation_id=conv["id"],
                      detail={"template": template_id, "attempt": i + 1, "violations": violations, "rejected_text": res.text[:400],
                              "fallback": "approved template" if i + 1 == attempts else "retry with violations"})
            req.violations = violations
        decision["composer_refused"] = req.violations
        return template_body, "template", fs

    # ---------------------------------------------------------------- version 4: emergency, clinical handoff, relay
    def _emergency(self, conv, patient, ps, msg_id, result, decided_by: str = "code") -> None:
        """Emergency wording: approved safety text with 911 and the nearest partner-verified urgent care (chosen by
        distance from the patient's location); conversation held; emergency-priority partner item when enabled.
        Goes out through every pause and quiet hour (kind=safety).  No model writes any of it."""
        now = self.now()
        cid = conv["id"]
        dk = "conv%d:%%s:msg%d" % (cid, msg_id)
        origin = self.patient_point(patient["id"]) or self.directory.town_coords(patient.get("home_town"))
        uc = self.directory.nearest_urgent_care(origin, now)
        # "nearest" only when a location (stored point or home town) exists; otherwise name it without the claim
        line = ((" For an urgent problem that is not an emergency, the nearest urgent care is %s, %s, %s (%s)." if origin else " For an urgent problem that is not an emergency, urgent care: %s, %s, %s (%s).")
                % (uc["name"], uc["address"], uc["phone"], uc.get("hours_text", ""))) if uc else \
               (" For urgent care today, call %s." % self.directory.clinician_contact.get("phone", "your clinic"))
        esc = None
        if self.policy.emergency_notify_partner:
            esc = self._escalate(conv, "emergency_wording", "Emergency wording (%s-decided): %s" % (decided_by, ps.text[:200]), msg_id,
                                 after_hours=not clinician_available(now, self.policy), priority="emergency")
        if conv["state"] not in ("closed",):
            self._set_conv_state(self._conv(cid), "waiting_partner", detail={"reason": "emergency_wording", "escalation_id": esc, "decided_by": decided_by})
            self._set_next(cid, None, None, reason="held: emergency wording; 911 / urgent-care text sent; a person decides what happens next")
        _refs.create(self, conv, "emergency", "emergency", msg_id, state="queued" if esc else "offered", delivery="simulated" if esc else "none", escalation_id=esc,
                     extra={"note": "911 guidance sent immediately; partner notification and the next-day follow-up are separate steps"})
        self.conn.execute("UPDATE conversations SET pause_reason='emergency' WHERE id=?", (cid,))
        self._queue(self._conv(cid), "emergency_ack", "safety", dedupe_key=dk % "emergency", extra={"urgent_care_line": line},
                    decision={"rule": "emergency_wording", "decided_by": decided_by, "urgent_care": (uc or {}).get("id"),
                              "reason": "code-decided safety text; nearest verified urgent care by distance from the patient's location"})
        log_event(self.conn, now.strftime(ISO), "system", "emergency_wording", conversation_id=cid, patient_id=patient["id"],
                  detail={"decided_by": decided_by, "urgent_care": (uc or {}).get("id"), "partner_item": esc})
        result.update(intent="emergency", template="emergency_ack", escalation=esc, routing="emergency")

    def capability(self, name: str) -> bool:
        """Partner-granted operational capability (V5-1), default deny."""
        return self.directory.granted(name)

    def _deny(self, conv, name: str, what: str) -> None:
        now = self.now().strftime(ISO)
        log_event(self.conn, now, "system", "capability_denied", conversation_id=conv["id"] if conv else None, detail={"capability": name, "action": what})
        if conv and not row(self.conn, "SELECT 1 FROM escalations WHERE conversation_id=? AND reason='capability_denied' AND status='open'", (conv["id"],)):
            self._escalate(conv, "capability_denied", "%s refused: the partner has not granted '%s'" % (what, name), None, keep_queued=True)

    def clinical_route(self) -> str:
        """Which clinical route exists for this partner: 'portal' (approved link, and the policy is a portal mode), 'queue'
        (the partner explicitly accepts our clinician queue: directory clinician_contact.accepts_queue AND the capability
        grant), or 'none'.  V4-3: a missing portal never silently opts a partner into the queue."""
        now = self.now()
        accepts_queue = bool(self.directory.clinician_contact.get("accepts_queue")) and self.capability("clinician_queue")
        if self.policy.clinical_handoff == "clinician_queue":
            return "queue" if accepts_queue else "none"
        if self.directory.portal(now):
            return "portal"
        return "queue" if accepts_queue else "none"

    def _clinical_route_missing(self, conv, patient, ps, msg_id, dk, kind: str, decision: Optional[Dict] = None) -> Tuple[Optional[str], Optional[int]]:
        """No portal link and no opted-in queue: give the clinic phone (no promise of a clinical response) and open a
        configuration item for Kate, once per conversation."""
        now = self.now()
        cid = conv["id"]
        esc = None
        if not row(self.conn, "SELECT 1 FROM escalations WHERE conversation_id=? AND reason='clinical_route_missing' AND status='open'", (cid,)):
            esc = self._escalate(conv, "clinical_route_missing", "Clinical %s with no configured clinical route (no approved portal link; partner has not opted into the clinician queue): %s" % (kind, ps.text[:160]), msg_id)
        self._queue(self._conv(cid), "clinical_call_instructions", "safety", dedupe_key=dk % "clinical_call",
                    decision=dict(decision or {}, rule="clinical_route_missing", reason="no clinical route configured; clinic phone only, no promised response"))
        _refs.create(self, conv, "route_missing", "staff_request" if kind == "staff" else "clinical_question", msg_id, state="offered", delivery="none", escalation_id=esc)
        log_event(self.conn, now.strftime(ISO), "system", "clinical_route_missing", conversation_id=cid, detail={"kind": kind})
        if conv["state"] in ("outreach_sent", "engaged", "plan_agreed", "new"):
            self._set_next(cid, "clinical_followup", now + timedelta(days=self.policy.clinical_pause_days), reason="clinical question with no configured route; quiet, then one follow-up")
        return "clinical_call_instructions", esc

    def _clinical_handoff(self, conv, patient, ps, msg_id, dk, kind: str, topic: Optional[str], decision: Optional[Dict] = None,
                          held: bool = False) -> Tuple[Optional[str], Optional[int]]:
        """Version 4 clinical routing (brief §1 and §6).  A preparation question with partner-approved prep text for every
        open line is answered by the application, verbatim, with the portal link for anything further.  Everything else:
          portal_link     approved text with the portal link and phone; no queue item; outreach pauses `clinical_pause_days`
          portal_relay    offer to send the patient's own words through the portal; the next text is the message
          clinician_queue version 3 behaviour (caller handles; this method is not reached)
        Returns (template, escalation_id)."""
        now = self.now()
        cid = conv["id"]
        dec = dict(decision or {})
        codes = self._open_codes(patient["id"])
        if kind == "question" and topic == "prep":
            prep = self.catalog.prep_instruction(codes, now)
            if prep:
                self._queue(self._conv(cid), "prep_answer", "reply", dedupe_key=dk % "prep", extra={"prep_instruction": prep},
                            decision=dict(dec, rule="prep_question→approved_prep_text", reason="partner-approved preparation text, sent verbatim; not model-written"))
                log_event(self.conn, now.strftime(ISO), "system", "clinical_self_served", conversation_id=cid, detail={"topic": "prep", "codes": codes})
                return "prep_answer", None
        # version 5: "why was this ordered?" is answered ONLY from an approved documented card (the note's own words, attributed)
        # or with an honest gap; either way it is never inferred.  A gap is a referral the record cannot resolve.
        rationale_gap = False
        if kind == "question" and topic == "rationale":
            rat = _facts.rationale_for_reply(self, patient["id"])
            if rat["documented"]:
                d0 = rat["documented"][0]
                try:
                    date_text = datetime.fromisoformat(d0["authored_at"][:19]).strftime("%b %-d") if d0.get("authored_at") else "the visit"
                except ValueError:
                    date_text = "the visit"
                self._queue(self._conv(cid), "rationale_documented", "reply", dedupe_key=dk % "rationale",
                            extra={"rationale_author": d0.get("author") or "The clinician", "rationale_date": date_text, "rationale_excerpt": d0["excerpt"]},
                            decision=dict(dec, rule="rationale→approved_documented_card", card_id=d0["card_id"], reason="verbatim excerpt from an approved documented fact card; attributed; no interpretation"))
                log_event(self.conn, now.strftime(ISO), "system", "clinical_self_served", conversation_id=cid, detail={"topic": "rationale", "card_id": d0["card_id"]})
                return "rationale_documented", None
            rationale_gap = True
        mode = self.policy.clinical_handoff
        if rationale_gap:
            tpl = "rationale_unknown"
        elif mode == "portal_relay" and not held and self.capability("portal_relay"):
            tpl = "clinical_relay_offer"
            self.conn.execute("UPDATE conversations SET relay_pending=? WHERE id=?",
                              (json.dumps({"kind": kind, "message_id": msg_id, "at": now.strftime(ISO)}), cid))
        else:
            if mode == "portal_relay" and not held and not self.capability("portal_relay"):
                log_event(self.conn, now.strftime(ISO), "system", "capability_denied", conversation_id=cid, detail={"capability": "portal_relay", "action": "relay offer", "fallback": "portal link"})
            tpl = "staff_portal_link" if kind == "staff" else "clinical_portal_link"
        self.conn.execute("UPDATE conversations SET clinical_referrals=clinical_referrals+1, pause_reason='clinical_referral' WHERE id=?", (cid,))
        self._queue(self._conv(cid), tpl, "safety", dedupe_key=dk % ("relay" if tpl == "clinical_relay_offer" else "portal"),
                    decision=dict(dec, rule="clinical_handoff:%s" % mode, kind=kind, topic=topic, rationale_gap=rationale_gap,
                                  reason="clinical content is the care team's; the patient is pointed to (or relayed through) the partner's own portal"))
        log_event(self.conn, now.strftime(ISO), "system", "clinical_portal_referral", conversation_id=cid, patient_id=patient["id"],
                  detail={"mode": mode, "kind": kind, "topic": topic, "held": held, "template": tpl})
        reason = "staff_request" if kind == "staff" else "clinical_question"
        if tpl == "clinical_relay_offer":
            rid = _refs.create(self, conv, "portal_relay", reason, msg_id, topic=topic, state="awaiting_patient", delivery="none")
            self.conn.execute("UPDATE conversations SET relay_pending=? WHERE id=?", (json.dumps({"kind": kind, "message_id": msg_id, "at": now.strftime(ISO), "referral_id": rid}), cid))
        else:
            _refs.create(self, conv, "portal_link", reason, msg_id, topic=topic, state="offered", delivery="unverified",
                         extra={"note": "the patient was given the portal link; nothing was sent by us; delivery to the provider is unverified until evidence arrives"})
        if not held and conv["state"] in ("outreach_sent", "engaged", "plan_agreed", "new"):
            # not a hold: the provider answers in the portal; we go quiet, then one gentle follow-up (brief §1)
            self._set_next(cid, "clinical_followup", now + timedelta(days=self.policy.clinical_pause_days),
                           reason="clinical question referred to the portal; quiet for %d days, then one gentle follow-up" % self.policy.clinical_pause_days)
        return tpl, None

    def _relay_capture(self, conv, patient, ps, msg_id, result) -> None:
        """The text after a relay offer IS the patient's message to their provider (verbatim), unless it declines."""
        now = self.now()
        cid = conv["id"]
        dk = "conv%d:%%s:msg%d" % (cid, msg_id)
        pending = json.loads(conv["relay_pending"] or "{}")
        text = ps.text.strip()
        if _re.match(r"^\W*(no|nope|never ?mind|nevermind|forget it|don'?t|do not|not now|skip|cancel that)\b", text, _re.IGNORECASE):
            self.conn.execute("UPDATE conversations SET relay_pending=NULL WHERE id=?", (cid,))
            if pending.get("referral_id"):
                _refs.transition(self, pending["referral_id"], "cancelled", detail={"reason": "patient declined the relay"})
            self._queue(self._conv(cid), "clinical_relay_cancelled", "reply", dedupe_key=dk % "relay_cancel",
                        decision={"rule": "relay_declined", "pending": pending})
            log_event(self.conn, now.strftime(ISO), "system", "portal_relay_cancelled", conversation_id=cid)
            result.update(intent="relay_cancelled", template="clinical_relay_cancelled")
            return
        # V4-2: an acknowledgement ("yes please", "ok", "sure") is consent, not the message.  Ask for the message itself; the
        # offer stays open.  Only a substantive text (several words, or a question) is sent, and it is sent exactly once.
        words = _re.findall(r"[A-Za-z']+", text)
        ack = bool(_re.match(r"^\W*(yes|yeah|yep|yup|ok|okay|sure|please|fine|go ahead|do it|send it|that'?s fine|sounds good)\b", text, _re.IGNORECASE)) and len(words) <= 4
        if ack or len(words) < 4 and "?" not in text:
            prompts = int(pending.get("prompts", 0)) + 1
            if prompts > 2:
                self.conn.execute("UPDATE conversations SET relay_pending=NULL WHERE id=?", (cid,))
                self._queue(self._conv(cid), "clinical_relay_cancelled", "reply", dedupe_key=dk % "relay_giveup", decision={"rule": "relay_no_message_after_two_prompts", "pending": pending})
                result.update(intent="relay_cancelled", template="clinical_relay_cancelled")
                return
            pending["prompts"] = prompts
            self.conn.execute("UPDATE conversations SET relay_pending=? WHERE id=?", (json.dumps(pending), cid))
            self._queue(self._conv(cid), "clinical_relay_prompt", "reply", dedupe_key=dk % "relay_prompt", decision={"rule": "relay_ack_needs_message", "pending": pending})
            result.update(intent="relay_ack", template="clinical_relay_prompt")
            return
        self.conn.execute("UPDATE conversations SET relay_pending=NULL WHERE id=?", (cid,))
        if row(self.conn, "SELECT id FROM portal_messages WHERE inbound_message_id=?", (msg_id,)):
            log_event(self.conn, now.strftime(ISO), "system", "portal_relay_duplicate_prevented", conversation_id=cid, detail={"message_id": msg_id})
            result.update(intent="relay_sent", template=None)
            return
        o = row(self.conn, "SELECT ordering_provider FROM orders WHERE patient_id=? ORDER BY id LIMIT 1", (patient["id"],)) or {}
        provider = o.get("ordering_provider") or "care team"
        refs = [r["source_order_id"] for r in self._open_orders(patient["id"])]
        subject = "Question about lab order%s %s (sent via text assistant)" % ("s" if len(refs) > 1 else "", ", ".join(refs) or "(none open)")
        original = (row(self.conn, "SELECT body FROM messages WHERE id=?", (pending.get("message_id"),)) or {}).get("body")
        cur = self.conn.execute("INSERT INTO portal_messages(conversation_id,patient_id,inbound_message_id,provider,subject,body,order_refs,adapter,status,created_at,context_question) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (cid, patient["id"], msg_id, provider, subject, text, json.dumps(refs),
                                                                  self.portal.name, "sending", now.strftime(ISO), original))
        pmid = cur.lastrowid
        self.conn.commit()          # commit-before-call, as with SMS: a crash after this point leaves `sending`, reconciled at the next tick
        try:
            res = self.portal.send_patient_message(patient["source_patient_id"], provider, subject, text, refs)
        except PortalError as e:
            self.conn.execute("UPDATE portal_messages SET status='failed', last_error=? WHERE id=?", (str(e), pmid))
            if pending.get("referral_id"):
                _refs.transition(self, pending["referral_id"], "failed", delivery="failed", detail={"error": str(e), "portal_message": pmid})
            esc = self._escalate(conv, "human_request", "Portal relay failed (%s); the patient's message is stored verbatim on portal message #%d" % (e, pmid), msg_id)
            self._queue(self._conv(cid), "human_ack", "reply", dedupe_key=dk % "relay_failed", decision={"rule": "relay_failed→human", "portal_message": pmid})
            result.update(intent="relay_failed", template="human_ack", escalation=esc)
            return
        self.conn.execute("UPDATE portal_messages SET status=?, provider_message_id=?, sent_at=? WHERE id=?",
                          ("sent" if not res.simulated else "sent_simulated", res.provider_message_id, now.strftime(ISO), pmid))
        self._queue(self._conv(cid), "clinical_relay_sent", "reply", dedupe_key=dk % "relay_sent",
                    decision={"rule": "relay_sent", "portal_message": pmid, "adapter": res.adapter, "simulated": res.simulated,
                              "reason": "the patient's own words, verbatim, as a patient-authored portal message; nothing model-written"})
        log_event(self.conn, now.strftime(ISO), "system", "portal_relay_sent", conversation_id=cid, patient_id=patient["id"],
                  detail={"portal_message": pmid, "adapter": res.adapter, "simulated": res.simulated, "chars": len(text), "original_question_kept": bool(original)})
        if pending.get("referral_id"):
            self.conn.execute("UPDATE referrals SET portal_message_id=? WHERE id=?", (pmid, pending["referral_id"]))
            _refs.transition(self, pending["referral_id"], "sent", delivery="simulated" if res.simulated else "unverified",
                             detail={"portal_message": pmid, "adapter": res.adapter, "note": "sent is not received; delivery evidence must come from the partner"},
                             next_action="await partner acknowledgement or response evidence")
        self.conn.execute("UPDATE conversations SET pause_reason='relay_sent' WHERE id=?", (cid,))
        if self._conv(cid)["state"] in ("outreach_sent", "engaged", "plan_agreed"):
            self._set_next(cid, "clinical_followup", now + timedelta(days=self.policy.relay_followup_days), reason="relay sent; one check-in in %d days" % self.policy.relay_followup_days)
        result.update(intent="relay_sent", template="clinical_relay_sent", portal_message=pmid)

    def _do_clinical_followup(self, conv: Dict) -> str:
        """One gentle follow-up after a portal referral (approved wording).  Then the ordinary cadence resumes."""
        conv = self._conv(conv["id"])
        if conv["state"] not in ("outreach_sent", "engaged", "plan_agreed") or not self._open_orders(conv["patient_id"]):
            self._set_next(conv["id"], None, None)
            return "skip:%d:clinical_followup" % conv["id"]
        if self.paused():
            return "held:%d:%s" % (conv["id"], self.paused())
        tpl = "relay_followup" if conv.get("pause_reason") == "relay_sent" else "clinical_followup_after_referral"
        queued = self._queue(conv, tpl, "scheduled", dedupe_key="conv%d:ep%d:%s:%d" % (conv["id"], conv["episode"], tpl, conv["clinical_referrals"]),
                             decision={"rule": tpl, "pause_reason": conv.get("pause_reason"), "reason": "follow-up chosen by the reason for the pause (v5)"})
        self.conn.execute("UPDATE conversations SET pause_reason=NULL WHERE id=?", (conv["id"],))
        self._set_next(conv["id"], "followup", self.now() + timedelta(days=self.policy.followup_interval_days),
                       reason="after the post-referral follow-up, the ordinary cadence resumes")
        return "clinical_followup:%d%s" % (conv["id"], "" if queued else ":dedupe_hit")

    def import_notes(self, feed: Dict) -> Dict:
        """Notes enter only under a granted record-read capability (V5-1)."""
        if not self.capability("record_read"):
            log_event(self.conn, self.now().strftime(ISO), "system", "capability_denied", detail={"capability": "record_read", "action": "notes import"})
            self.conn.commit()
            return {"ok": False, "reason": "capability 'record_read' not granted", "notes_created": 0}
        r = _facts.import_notes(self.conn, feed, self.now())
        self.conn.commit()
        return dict(r, ok=True)

    # ---------------------------------------------------------------- version 5: longitudinal orders, booking
    def _plan_change_reported(self, conv, patient, msg_id, dk, dec) -> Tuple[Optional[str], Optional[int]]:
        """The patient says the provider changed or dropped the plan.  Pause affected outreach, open reconciliation, record the
        report on the order history — and never change the prescribed target.  Only a provider/order event does that."""
        now = self.now()
        cid = conv["id"]
        for o in self._open_orders(patient["id"]):
            importer.record_order_event(self.conn, o["id"], patient["id"], None, "patient_reported_change", now.strftime(ISO), "patient",
                                        {"message_id": msg_id, "note": "report only; target unchanged pending an authorized order event"}, now)
        esc = self._escalate(conv, "plan_change_reconcile", "Patient reports the provider changed or dropped the plan: %s" % (row(self.conn, "SELECT body FROM messages WHERE id=?", (msg_id,)) or {}).get("body", "")[:160], msg_id)
        bk = _sched.active_booking(self.conn, cid)
        if bk:
            self._cancel_booking(conv, patient, msg_id, "plan change reported; booking released pending reconciliation", quiet=True)
        self._set_conv_state(self._conv(cid), "waiting_partner", detail={"reason": "plan_change_reported", "escalation_id": esc})
        self.conn.execute("UPDATE conversations SET pause_reason='plan_change_reported' WHERE id=?", (cid,))
        self._set_next(cid, None, None, reason="held: patient-reported plan change; waiting for the partner's order event, no restart on a timer")
        self._queue(self._conv(cid), "plan_change_ack", "reply", dedupe_key=dk % "planchange", decision=dict(dec, rule="plan_change_reported", reason="pause + reconcile; the target is never changed by a patient report"))
        _refs.create(self, conv, "plan_change", "plan_change_reported", msg_id, state="queued", delivery="simulated", escalation_id=esc)
        return "plan_change_ack", esc

    def _reconcile_after_provider_event(self, patient_id: int, changes: List) -> None:
        """An authorized order event for a patient whose outreach is paused on a reported plan change: resolve the
        reconciliation, tell the patient what is on file now (approved wording), and resume only if orders remain open.
        V5-4: only an event that can actually settle the discrepancy counts — a cancellation, replacement, modification or
        result dated AFTER the patient's report; attendance or older events are recorded but do not resume."""
        conv = row(self.conn, "SELECT * FROM conversations WHERE patient_id=?", (patient_id,))
        if not conv or conv.get("pause_reason") != "plan_change_reported":
            return
        now = self.now()
        report = row(self.conn, "SELECT MAX(at) a FROM order_events WHERE patient_id=? AND kind='patient_reported_change'", (patient_id,)) or {}
        resolving = [c for c in changes if c[1] in ("cancelled_by_partner", "replaced", "modified", "verified_complete") and (not c[2] or not report.get("a") or c[2] >= report["a"])]
        if not resolving:
            log_event(self.conn, now.strftime(ISO), "system", "reconciliation_not_resolved", patient_id=patient_id, conversation_id=conv["id"],
                      detail={"events": [(c[0], c[1], c[2]) for c in changes], "note": "attendance or an event older than the patient's report does not settle a reported plan change"})
            return
        changes = [c[1] for c in resolving]
        for e in rows(self.conn, "SELECT id FROM escalations WHERE conversation_id=? AND reason='plan_change_reconcile' AND status='open'", (conv["id"],)):
            self._resolve(e["id"], "partner_feed", "reconciled by provider order event(s): %s" % ", ".join(changes), 0.0, "none", resume=False)
        for r in rows(self.conn, "SELECT id FROM referrals WHERE conversation_id=? AND kind='plan_change' AND state NOT IN ('resolved','cancelled')", (conv["id"],)):
            _refs.transition(self, r["id"], "responded", actor="partner_feed", delivery="confirmed", detail={"changes": changes})
            _refs.resolve(self, r["id"], "system", "operator", "provider order event received: %s" % ", ".join(changes), "partner_feed", outcome="reconciled")
        self.conn.execute("UPDATE conversations SET pause_reason=NULL WHERE id=?", (conv["id"],))
        open_orders = self._open_orders(patient_id)
        conv = self._conv(conv["id"])
        if not open_orders:
            if conv["state"] != "closed":
                self._close_conversation(conv, "orders_terminal")
            return
        if conv["state"] in HELD_STATES:
            for o in open_orders:
                if o["state"] in ("escalated",):
                    self._set_order_state(o, "outreach_active", actor="partner_feed", detail={"reason": "reconciled"})
            self._set_conv_state(conv, "engaged", detail={"reason": "reconciled after provider event"})
        summary = "; ".join("%s (%s)" % (", ".join(l["test_name"] for l in rows(self.conn, "SELECT test_name FROM order_lines WHERE order_id=? AND status='outstanding'", (o["id"],))), o["source_order_id"]) for o in open_orders)
        self._queue(self._conv(conv["id"]), "plan_change_confirmed", "reply", dedupe_key="conv%d:ep%d:planchange_confirmed:%s" % (conv["id"], conv["episode"], now.strftime("%Y%m%d%H%M")),
                    extra={"order_summary": summary}, decision={"rule": "plan_change_confirmed", "changes": changes, "reason": "authorized order event reconciled the target; approved wording"})
        self._set_next(conv["id"], "followup", now + timedelta(days=self.policy.followup_interval_days), reason="resumed after reconciliation")

    def _slots_for(self, site: Dict, date, eff: Dict, limit: int = 2) -> List[str]:
        """Open times that also fit the patient's stated bounds (V5-6) and the timed-test start limit."""
        req = self._requirements_for_site_check(site)
        latest = self.directory.latest_start(site, req, default=self.catalog.timing(req).get("latest_start"))
        out = []
        for slot in self.scheduler.availability(site, date, self.now(), limit=12):
            hhmm = slot[11:16]
            if eff.get("after_time") and hhmm < eff["after_time"]:
                continue
            if eff.get("before_time") and hhmm >= eff["before_time"]:
                continue
            if latest and hhmm > latest:
                continue
            out.append(slot)
            if len(out) >= limit:
                break
        return out

    def _requirements_for_site_check(self, site: Dict) -> set:
        return set()

    def _book_slot(self, conv, patient, idx: int, msg_id) -> Optional[str]:
        now = self.now()
        cid = conv["id"]
        dk = "conv%d:%%s:msg%d" % (cid, msg_id)
        ps = json.loads(conv["pending_slots"] or "{}")
        slots = ps.get("slots") or []
        site = self.directory.site(ps.get("site_id"), now)
        self.conn.execute("UPDATE conversations SET pending_slots=NULL WHERE id=?", (cid,))
        # V5-6 revalidation at booking time: the slot must still fit the patient's current bounds, the order must be open,
        # and the lead time must still hold
        eff = self.active_preferences(patient["id"])
        if idx < len(slots) and site:
            hhmm = slots[idx][11:16]
            stale = (eff.get("after_time") and hhmm < eff["after_time"]) or (eff.get("before_time") and hhmm >= eff["before_time"]) \
                or datetime.fromisoformat(slots[idx]) < now + timedelta(hours=self.scheduler.lead_time_hours) or not self._open_orders(patient["id"])
            if stale:
                log_event(self.conn, now.strftime(ISO), "system", "slot_revalidation_failed", conversation_id=cid, detail={"slot": slots[idx], "constraints": eff})
                site = None
        if not site or idx >= len(slots):
            self._queue(self._conv(cid), "booking_failed_walkin", "reply", dedupe_key=dk % "bookfail", extra={"site_name": (site or {}).get("name", ""), "site_address": (site or {}).get("address", ""), "when": ps.get("when", "")},
                        decision={"rule": "booking_failed", "reason": "slot list or site no longer valid"})
            return "booking_failed_walkin"
        slot = slots[idx]
        refs = [o["source_order_id"] for o in self._open_orders(patient["id"])]
        oids = [o["id"] for o in self._open_orders(patient["id"])]
        try:
            conf = self.scheduler.book(site["id"], slot, patient["source_patient_id"], refs)
        except SchedulingError as e:
            _sched.record(self.conn, now, cid, patient["id"], oids, site["id"], slot, None, self.scheduler.name, "failed", error=str(e))
            self._queue(self._conv(cid), "booking_failed_walkin", "reply", dedupe_key=dk % "bookfail", extra={"site_name": site["name"], "site_address": site["address"], "when": ps.get("when", "")},
                        site_ids=[site["id"]], decision={"rule": "booking_failed", "error": str(e), "reason": "the walk-in plan stands; never described as booked"})
            return "booking_failed_walkin"
        bid = _sched.record(self.conn, now, cid, patient["id"], oids, site["id"], slot, conf, self.scheduler.name, "booked")
        self.conn.execute("UPDATE conversations SET booking_id=? WHERE id=?", (bid, cid))
        self._queue(self._conv(cid), "booking_confirmed", "reply", dedupe_key=dk % "booked", site_ids=[site["id"]],
                    extra={"when": ps.get("when", ""), "slot_time": _short_time(slot[11:16]), "site_name": site["name"], "site_address": site["address"], "confirmation_id": conf},
                    decision={"rule": "booking_confirmed", "booking_id": bid, "adapter": self.scheduler.name, "simulated": self.scheduler.simulated,
                              "reason": "a confirmed booking through the (simulated) scheduling adapter; the word 'booked' is allowed only because the adapter established it"})
        return "booking_confirmed"

    def _walk_in_instead(self, conv, patient, msg_id) -> str:
        now = self.now()
        cid = conv["id"]
        dk = "conv%d:%%s:msg%d" % (cid, msg_id)
        ps = json.loads(conv["pending_slots"] or "{}")
        self.conn.execute("UPDATE conversations SET pending_slots=NULL WHERE id=?", (cid,))
        site = self.directory.site(ps.get("site_id"), now) or {}
        self._queue(self._conv(cid), "plan_confirmed", "reply", dedupe_key=dk % "plan", extra={"when": ps.get("when", ""), "site_name": site.get("name", "")},
                    site_ids=[site["id"]] if site else None, decision={"rule": "walk_in_instead_of_booking", "reason": "the patient chose a walk-in plan; an intention, not an appointment"})
        return "plan_confirmed"

    def _cancel_booking(self, conv, patient, msg_id, why: str, quiet: bool = False) -> Optional[str]:
        now = self.now()
        cid = conv["id"]
        bk = _sched.active_booking(self.conn, cid)
        if not bk:
            return None
        try:
            self.scheduler.cancel(bk["site_id"], bk["slot_at"], bk["confirmation_id"])
        except SchedulingError as e:
            # V5-7: the reservation still stands; say so, keep the ledger true, and let a person reconcile
            log_event(self.conn, now.strftime(ISO), "system", "booking_cancel_failed", conversation_id=cid, detail={"booking_id": bk["id"], "error": str(e)})
            _sched.set_status(self.conn, now, bk["id"], "cancel_pending", error=str(e))
            esc = self._escalate(conv, "booking_change_failed", "Scheduler refused to cancel booking %s (%s); the booking still stands" % (bk["confirmation_id"], e), msg_id, keep_queued=True)
            if not quiet:
                site = self.directory.site(bk["site_id"], now) or {}
                self._queue(self._conv(cid), "booking_move_failed", "reply", dedupe_key="conv%d:booking_move_failed:msg%s" % (cid, msg_id),
                            extra={"site_name": site.get("name", ""), "slot_time": _short_time(bk["slot_at"][11:16]), "when": self._conv(cid)["agreed_when"] or "", "confirmation_id": bk["confirmation_id"]},
                            decision={"rule": "booking_change_failed", "escalation_id": esc, "reason": "never tell the patient a booking is cancelled when the adapter refused"})
            return "booking_move_failed"
        _sched.set_status(self.conn, now, bk["id"], "cancelled", error=why)
        self.conn.execute("UPDATE conversations SET booking_id=NULL WHERE id=?", (cid,))
        if quiet:
            return None
        self._clear_plan(self._conv(cid))
        self._set_conv_state(self._conv(cid), "engaged", detail={"reason": why})
        self._set_next(cid, "followup", now + timedelta(days=self.policy.followup_interval_days), reason="booking cancelled; ask again if no plan")
        self._queue(self._conv(cid), "booking_moved", "reply", dedupe_key="conv%d:booking_moved:msg%s" % (cid, msg_id), decision={"rule": "booking_cancelled", "why": why})
        return "booking_moved"

    def _record_attendance(self, o: Dict) -> None:
        """A partner `attended` event: attendance evidence on the booking (or a walk-in plan), never completion."""
        now = self.now()
        conv = row(self.conn, "SELECT * FROM conversations WHERE patient_id=?", (o["patient_id"],))
        if not conv:
            return
        bk = _sched.active_booking(self.conn, conv["id"])
        if bk:
            _sched.set_status(self.conn, now, bk["id"], "attended")
        else:
            log_event(self.conn, now.strftime(ISO), "partner_feed", "attended_without_booking", conversation_id=conv["id"], patient_id=o["patient_id"],
                      detail={"order_id": o["id"], "note": "walk-in attendance evidence; completion still requires a result"})

    # ---------------------------------------------------------------- version 4: the operational resolver
    def _resolver_note(self, dec: Dict) -> str:
        r = dec.get("resolver")
        if not r:
            return ""
        return "\nResolver tried: %s → %s (%s); reviewer: %s" % (", ".join(o["id"] for o in r.get("options", [])) or "no options",
                                                                 r.get("chosen"), r.get("rationale"), r.get("verdict"))

    def _build_options(self, conv, patient, kind: str, eff: Dict) -> List[Dict]:
        """The application-verified menu.  Every option here may be executed as-is; the model only picks."""
        now = self.now()
        req = self._requirements(patient["id"])
        town = eff.get("town") or patient.get("home_town")
        origin = self.patient_point(patient["id"]) or self.directory.town_coords(town)
        ls = self.catalog.timing(req).get("latest_start")
        opts: List[Dict] = []
        if kind in ("schedule", "transport"):
            base = {k: eff[k] for k in ("after_time", "before_time", "weekday", "weekend_ok") if eff.get(k)}
            hard = set(eff.get("hard") or [])
            relaxations = []
            # V4-1: a constraint the patient called absolute is never a candidate for relaxation
            if base.get("weekday") and "weekday" not in hard:
                relaxations.append(("weekday", {k: v for k, v in base.items() if k != "weekday"}))
            if base.get("after_time") and "after_time" not in hard:
                relaxations.append(("after_time", {k: v for k, v in base.items() if k != "after_time"}))
            if base.get("before_time") and "before_time" not in hard:
                relaxations.append(("before_time", {k: v for k, v in base.items() if k != "before_time"}))
            if base.get("weekend_ok") and not base.get("weekday") and "weekend_ok" not in hard:
                relaxations.append(("weekend_ok", {k: v for k, v in base.items() if k != "weekend_ok"}))
            seen = set()
            for i, (relaxed, c) in enumerate(relaxations):
                # the same "evenings OR weekends" handling as the ordinary offer path
                sites = self._filter_sites(dict(c, after_time_derived=eff.get("after_time_derived")), now, town, req)[:2] if c \
                    else self.directory.nearest(town, now, 2, requirements=req, point=origin)
                key = tuple(x["id"] for x in sites)
                if sites and key not in seen:
                    seen.add(key)
                    opts.append({"id": "alt-%d" % (i + 1), "kind": "sites_relaxed", "relaxed": [relaxed], "kept": c,
                                 "sites": [{"id": x["id"], "name": x["name"], "town": x.get("town"), "hours": self.directory.hours_for(x, c.get("weekday"), bool(c.get("weekend_ok"))),
                                            "miles": self.directory.miles_from(origin, x)} for x in sites]})
        # no car: what matters is where the patient LIVES, not a town they mentioned ("can't get to Bath")
        stop_town = patient.get("home_town") if kind == "transport" else town
        for st in self.directory.mobile_stops(now, req, town=stop_town, origin=(self.patient_point(patient["id"]) or self.directory.town_coords(stop_town)) if kind == "transport" else origin):
            # for a schedule dead end the stop must itself fit what the patient said; for transport / out-of-area, nearness is the point
            fits = True
            if kind == "schedule":
                if eff.get("weekday") and eff["weekday"] != st["weekday"]:
                    fits = False
                if eff.get("weekend_ok") and not eff.get("weekday") and st["weekday"] not in ("sat", "sun"):
                    fits = False
                if eff.get("after_time") and not st["window"][1] > eff["after_time"]:
                    fits = False
                if eff.get("before_time") and not st["window"][0] < eff["before_time"]:
                    fits = False
            same_town = (st["town"] or "").lower() == (stop_town or "").lower()
            if kind == "transport" and not same_town:
                fits = False          # no car: a stop six miles away is not a solution; the ride program is
            if fits:
                opts.append({"id": "mobile-%s" % st["id"], "kind": "mobile_stop", "route_name": st["route_name"], "town": st["town"], "weekday": st["weekday"],
                             "window": st["window"], "address": st["address"], "miles": st["miles"], "same_town": same_town})
        if kind == "transport" and self.directory.instruction("transport", now):
            opts.append({"id": "ride", "kind": "transport_instruction", "text": self.directory.instruction("transport", now)})
        if kind == "out_of_area":
            near = self.directory.nearest(None, now, 1, requirements=req, point=origin)
            if near:
                mi = self.directory.miles_from(origin, near[0])
                if mi is not None and mi <= self.policy.out_of_area_miles * 1.5:
                    # just outside the radius: offering the nearest site is still reasonable; far outside, it is not an option
                    opts.append({"id": "nearest", "kind": "nearest_anyway", "site": {"id": near[0]["id"], "name": near[0]["name"], "town": near[0].get("town"), "miles": mi}})
        opts.append({"id": ESCALATE, "kind": ESCALATE})
        return opts

    def _record_model_call(self, conv, purpose: str, obj) -> None:
        if getattr(obj, "simulated", True):
            log_event(self.conn, self.now().strftime(ISO), "%s:%s" % (purpose, getattr(obj, "resolver", None) or getattr(obj, "reviewer", None) or "rules"),
                      "%s_call" % purpose, conversation_id=conv["id"], detail={"simulated": True})
            return
        self._record_attempts(conv, [{"adapter": getattr(obj, "resolver", None) or getattr(obj, "reviewer", None), "model": obj.model, "simulated": False, "outcome": "ok",
                                      "input_tokens": obj.input_tokens, "output_tokens": obj.output_tokens, "cache_read_tokens": obj.cache_read_tokens,
                                      "latency_ms": obj.latency_ms}], purpose=purpose)

    def _try_resolve(self, conv, patient, kind: str, eff: Dict, msg_id, dk, dec: Dict) -> Optional[Tuple[Optional[str], Optional[int]]]:
        """Before a Kate item opens for a solvable dead end: build the menu, let the resolver pick, let the reviewer check,
        execute on agreement.  Returns the (template, escalation) tuple when it handled the reply, else None (caller
        escalates; `dec['resolver']` carries what was tried).  `shadow` records and never acts; `off` skips."""
        mode = self.policy.resolver_mode
        if mode == "off" or kind not in RESOLVABLE_KINDS:
            return None
        now = self.now()
        cid = conv["id"]
        live = not getattr(self.resolver, "simulated", True) or not getattr(self.reviewer, "simulated", True)
        if live and self.spend_cap_exceeded():
            dec["resolver"] = {"skipped": "spend_cap"}
            log_event(self.conn, now.strftime(ISO), "system", "resolver_skipped", conversation_id=cid, detail={"reason": "spend_cap", "kind": kind})
            return None
        options = self._build_options(conv, patient, kind, eff)
        said = [m["body"] for m in rows(self.conn, "SELECT body FROM messages WHERE conversation_id=? AND direction='inbound' ORDER BY id DESC LIMIT 4", (cid,))]
        case = ResolveCase(kind=kind, patient_said=said, constraints=dict(eff), options=options)
        record: Dict = {"kind": kind, "options": [{"id": o["id"], "kind": o["kind"]} for o in options], "mode": mode}
        try:
            res = self.resolver.resolve(case)
            self._record_model_call(conv, "resolve", res)
            record.update(chosen=res.option_id, rationale=res.rationale, confidence=res.confidence, resolver=res.resolver)
            chosen = next((o for o in options if o["id"] == res.option_id), None)
            if chosen is None or chosen["kind"] == ESCALATE:
                record["verdict"] = "n/a (escalate)"
                dec["resolver"] = record
                log_event(self.conn, now.strftime(ISO), "system", "resolver_decision", conversation_id=cid, detail=dict(record, executed=False))
                return None
            verdict = self.reviewer.review(case, res)
            self._record_model_call(conv, "review", verdict)
            record.update(verdict="agree" if verdict.agree else "disagree", verdict_reason=verdict.reason, reviewer=verdict.reviewer)
        except ProviderError as e:
            record.update(error=str(e)[:200])
            dec["resolver"] = record
            log_event(self.conn, now.strftime(ISO), "system", "resolver_failed", conversation_id=cid, detail=record)
            return None
        dec["resolver"] = record
        if mode == "shadow":
            log_event(self.conn, now.strftime(ISO), "system", "resolver_decision", conversation_id=cid, detail=dict(record, executed=False, shadow=True))
            return None
        if not verdict.agree:
            log_event(self.conn, now.strftime(ISO), "system", "resolver_decision", conversation_id=cid, detail=dict(record, executed=False))
            esc = self._escalate(conv, "resolver_disagreement", "Resolver chose %s (%s); reviewer disagreed (%s)" % (res.option_id, res.rationale, verdict.reason), msg_id)
            self._queue(self._conv(cid), "handoff_generic", "reply", dedupe_key=dk % "resolver_dis", decision=dict(dec, rule="resolver_disagreement→person"))
            return "handoff_generic", esc
        out = self._execute_option(conv, patient, chosen, eff, dk, dict(dec, rule="resolver:%s" % chosen["kind"]))
        if not out or not out[0]:
            record["executed"] = False; record["refused_at_execution"] = True
            dec["resolver"] = record
            log_event(self.conn, now.strftime(ISO), "system", "resolver_decision", conversation_id=cid, detail=dict(record, executed=False))
            return None
        log_event(self.conn, now.strftime(ISO), "system", "resolver_decision", conversation_id=cid, detail=dict(record, executed=True))
        return out

    def _ensure_followup(self, cid: int, why: str) -> None:
        """A reply the automation sent on its own (resolver offer, location offer) must not leave the conversation
        without a next step: if the cadence has nothing scheduled, ask again in the ordinary interval."""
        c = self._conv(cid)
        # a reminder / verification left over from an invalidated plan is not a next step for an engaged conversation
        if c["state"] in ("engaged", "outreach_sent") and c["next_action"] in (None, "reminder", "verify_deadline"):
            self._set_next(cid, "followup", self.now() + timedelta(days=self.policy.followup_interval_days), reason=why)

    def _execute_option(self, conv, patient, opt: Dict, eff: Dict, dk, dec: Dict) -> Tuple[Optional[str], Optional[int]]:
        out = self._execute_option_inner(conv, patient, opt, eff, dk, dec)
        if out and out[0]:
            self._ensure_followup(conv["id"], "resolver offered %s; ask again if no reply" % opt["kind"])
        return out

    def _execute_option_inner(self, conv, patient, opt: Dict, eff: Dict, dk, dec: Dict) -> Tuple[Optional[str], Optional[int]]:
        now = self.now()
        req = self._requirements(patient["id"])
        # V4-1 revalidation at execution, independent of menu construction: never act on a relaxed hard constraint
        if opt.get("kind") == "sites_relaxed" and set(opt.get("relaxed") or []) & set(eff.get("hard") or []):
            log_event(self.conn, now.strftime(ISO), "system", "resolver_option_refused", conversation_id=conv["id"],
                      detail={"option": opt["id"], "reason": "relaxes a constraint the patient called absolute", "hard": eff.get("hard")})
            return None, None
        if opt["kind"] == "sites_relaxed":
            sites = [s for s in (self.directory.site(x["id"], now) for x in opt["sites"]) if s and self.directory.capable(s, req)]
            if not sites:
                return None, None
            asked = {"weekday": "%s" % WEEKDAY_NAME.get(eff.get("weekday", ""), eff.get("weekday", "that day")),
                     "after_time": "after %s" % _short_time(eff["after_time"]) if eff.get("after_time") else "",
                     "before_time": "before %s" % _short_time(eff["before_time"]) if eff.get("before_time") else "",
                     "weekend_ok": "a weekend"}.get(opt["relaxed"][0], "that")
            asked_line = "No location is open %s." % (("on %ss" % asked) if opt["relaxed"][0] == "weekday" else asked)
            kept = opt.get("kept") or {}
            alt_line = "%s (%s)" % (sites[0]["name"], self.directory.hours_for(sites[0], kept.get("weekday"), bool(kept.get("weekend_ok"))))
            return self._offer(conv, sites, "offer_alternative", dk % "alt", extra={"asked_line": asked_line, "alt_line": alt_line},
                               dec=dict(dec, relaxed=opt["relaxed"], asked_line=asked_line))
        if opt["kind"] == "mobile_stop":
            self._queue(conv, "offer_mobile_stop", "reply", dedupe_key=dk % "mobile",
                        extra={"stop_town": opt["town"], "stop_day": WEEKDAY_NAME.get(opt["weekday"], opt["weekday"]),
                               "stop_window": "%s-%s" % (_short_time(opt["window"][0]), _short_time(opt["window"][1])), "stop_address": opt["address"],
                               "route_name": opt["route_name"]},
                        decision=dict(dec, mobile_stop=opt["id"], reason="verified mobile-route stop in or near the patient's town"))
            log_event(self.conn, now.strftime(ISO), "system", "mobile_stop_offered", conversation_id=conv["id"], detail={"stop": opt["id"]})
            return "offer_mobile_stop", None
        if opt["kind"] == "transport_instruction":
            self._queue(conv, "transport_resolved", "reply", dedupe_key=dk % "transport_res", extra={"transport_instruction": opt["text"]},
                        instructions=["transport"], decision=dict(dec, reason="approved ride program sent; no person needed unless the ride does not work"))
            return "transport_resolved", None
        if opt["kind"] == "nearest_anyway":
            site = self.directory.site(opt["site"]["id"], now)
            return self._offer(conv, [site] if site else [], "offer_sites_nearby", dk % "nearest_anyway", extra={"town": patient.get("home_town") or ""},
                               dec=dict(dec, reason="nearest capable site offered although outside the service radius"))
        return None, None

    # ---------------------------------------------------------------- version 4: where is the patient
    def patient_point(self, patient_id: int):
        """Most recent unexpired stored location for the patient (lat, lon), or None.  Precedence is recency, not
        specificity: a patient who shared a device location yesterday and names a town today means the town.  The home
        town from the feed is not stored here; it is the town centroid the ordinary offer path uses when nothing is stored."""
        now = self.now().strftime(ISO)
        r = row(self.conn, "SELECT lat, lon FROM patient_locations WHERE patient_id=? AND (expires_at IS NULL OR expires_at>?) ORDER BY id DESC LIMIT 1", (patient_id, now))
        return (r["lat"], r["lon"]) if r else None

    def record_location(self, patient_id: int, lat: float, lon: float, source: str, label: Optional[str], consent_note: str) -> int:
        now = self.now()
        cur = self.conn.execute("INSERT INTO patient_locations(patient_id,lat,lon,source,label,consent_note,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                                (patient_id, float(lat), float(lon), source, label, consent_note, now.strftime(ISO),
                                 (now + timedelta(days=self.policy.location_ttl_days)).strftime(ISO)))
        log_event(self.conn, now.strftime(ISO), "system", "location_recorded", patient_id=patient_id,
                  detail={"source": source, "label": label, "note": "coordinates stored with source and expiry; no street address"})
        return cur.lastrowid

    def _locate(self, conv, patient, mr, msg_id, dk, dec):
        """A zip / a geocodable place / 'not near home'.  Geocode → store with source → offer the nearest capable sites with
        distances; out of area → resolver, then Kate; nothing to geocode → the consent-based location link."""
        now = self.now()
        cid = conv["id"]
        req = self._requirements(patient["id"])
        z = mr.constraints.get("zip")
        label, geo, source = None, None, None
        if z:
            geo = self.geocoder.geocode(z); label, source = z, "patient_zip"
        elif mr.constraints.get("town"):
            geo = self.geocoder.geocode(mr.constraints["town"]); label, source = mr.constraints["town"], "patient_town"
        if geo:
            self.record_location(patient["id"], geo[0], geo[1], source, label, "patient message #%d" % msg_id)
            return self._offer_from_point(conv, patient, (geo[0], geo[1]), geo[2], source, msg_id, dk, dict(dec, constraints=mr.constraints))
        if mr.constraints.get("where") == "here" or z:
            if not self.policy.location_link_enabled:
                return None
            link = self._location_link(cid)
            self._queue(conv, "location_link_offer", "reply", dedupe_key=dk % "loclink", extra={"location_link": link},
                        decision=dict(dec, rule="location_link_offer", reason="patient is not near home and named no place we can map; consent-based one-time share"))
            return "location_link_offer", None
        return None

    def _offer_from_point(self, conv, patient, origin, label: str, source: str, msg_id, dk, dec: Dict):
        """The ONE service-area rule for every location source (zip, town, shared device location — V4-4): nearest capable
        sites from the point; beyond `out_of_area_miles` → resolver → out-of-area Kate item with the nearest site and distance."""
        now = self.now()
        cid = conv["id"]
        req = self._requirements(patient["id"])
        sites = self.directory.nearest(None, now, 2, requirements=req, point=origin)
        nearest_mi = self.directory.miles_from(origin, sites[0]) if sites else None
        if sites and nearest_mi is not None and nearest_mi > self.policy.out_of_area_miles:
            resolved = self._try_resolve(conv, patient, "out_of_area", dict(dec.get("constraints") or {}, location_label=label), msg_id, dk, dec)
            if resolved:
                return resolved
            esc = self._escalate(conv, "out_of_area", "Patient near %s (%s): nearest capable site %s is %s miles away (radius %s)%s"
                                 % (label, source, sites[0]["name"], nearest_mi, self.policy.out_of_area_miles, self._resolver_note(dec)), msg_id)
            self._queue(self._conv(cid), "out_of_area_ack", "reply", dedupe_key=dk % "ooa",
                        extra={"nearest_site": sites[0]["name"], "nearest_miles": str(int(round(nearest_mi)))},
                        decision=dict(dec, rule="out_of_area", location=label, source=source, nearest=sites[0]["id"], miles=nearest_mi))
            return "out_of_area_ack", esc
        out = self._offer(conv, sites, "offer_sites_nearby", dk % "offer_loc", extra={"town": label},
                          dec=dict(dec, rule="offer:nearest_to_location", location=label, source=source))
        self._ensure_followup(cid, "offered sites near the patient's %s location; ask again if no reply" % source)
        return out

    def _location_link(self, cid: int) -> str:
        token = uuid.uuid4().hex[:20]
        now = self.now()
        self.conn.execute("INSERT INTO location_links(token,conversation_id,created_at,expires_at) VALUES(?,?,?,?)",
                          (token, cid, now.strftime(ISO), (now + timedelta(hours=self.policy.location_link_ttl_hours)).strftime(ISO)))
        return "%s/where/%s" % (self.policy.public_base_url.rstrip("/"), token)

    def record_shared_location(self, token: str, lat: float, lon: float) -> Dict:
        """The consent page posted a location.  Single use, expiring, bound to one conversation; then re-offer from it."""
        now = self.now()
        lk = row(self.conn, "SELECT * FROM location_links WHERE token=?", (token,))
        if not lk or lk["used_at"] or lk["expires_at"] < now.strftime(ISO):
            return {"ok": False, "reason": "unknown, used or expired link"}
        try:
            lat, lon = float(lat), float(lon)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid coordinates"}
        conv = self._conv(lk["conversation_id"])
        self.conn.execute("UPDATE location_links SET used_at=? WHERE token=?", (now.strftime(ISO), token))
        self.record_location(conv["patient_id"], lat, lon, "patient_shared", "shared from phone", "consent page %s" % token[:6])
        patient = row(self.conn, "SELECT * FROM patients WHERE id=?", (conv["patient_id"],))
        req = self._requirements(patient["id"])
        sites = self.directory.nearest(None, now, 2, requirements=req, point=(lat, lon))
        tpl = None
        if conv["state"] in ("outreach_sent", "engaged"):
            dk = "conv%d:%%s:shared%s" % (conv["id"], token[:6])
            tpl, _ = self._offer_from_point(conv, patient, (lat, lon), "where you are", "patient_shared", None, dk, {"rule": "shared_location"})
            self._flush_outbox()
        self.conn.commit()
        return {"ok": True, "conversation_id": conv["id"], "offered": tpl, "nearest": [s["id"] for s in sites]}

    PERSON_TEMPLATES = {"handoff_generic", "human_ack", "cost_ack", "transport_ack", "no_site_matches", "no_capable_site", "out_of_area_ack",
                        "clinical_ack_business_hours", "clinical_ack_after_hours", "staff_ack_business_hours", "staff_ack_after_hours", "emergency_ack"}

    @classmethod
    def _planner_agrees(cls, proposed: str, chosen: Optional[str]) -> bool:
        """Shadow planner comparison (V4-6): exact template, or a NARROW family whose members mean the same thing to the
        patient and cost the same human work.  Templates that differ in disposition (a person involved or not) are never
        the same family: transport_ack ≠ transport_resolved, handoff_generic ≠ unclear, prep_answer ≠ clinical_handoff."""
        if not chosen:
            return proposed == "none"
        fam = {"offer_sites": {"offer_sites", "offer_sites_nearby", "offer_sites_constrained"},
               "clinical_handoff": {"clinical_portal_link", "clinical_relay_offer", "clinical_ack_business_hours", "clinical_ack_after_hours"},
               "staff_handoff": {"staff_portal_link", "staff_ack_business_hours", "staff_ack_after_hours"},
               "unclear": {"unclear", "unclear_menu"}}
        return chosen == proposed or chosen in fam.get(proposed, set())

    @classmethod
    def _planner_disposition_agrees(cls, proposed: str, chosen: Optional[str]) -> bool:
        """Coarser signal recorded separately: did both put a person in the loop, or neither?"""
        person_proposed = proposed in {"human_ack", "cost_ack", "transport_ack", "clinical_handoff", "staff_handoff", "emergency_ack"}
        return person_proposed == (chosen in cls.PERSON_TEMPLATES)

    def _flush_outbox(self) -> List[str]:
        """Commit-before-call send protocol.  Revalidates every queued message against the current
        conversation state (epoch), pause, quiet hours, feed freshness, contact authorization, recipient
        identity and the directory content it depends on.  Pending inbound is drained first (no sends) so a
        durable STOP is applied before anything leaves."""
        now = self.now()
        out: List[str] = []
        out.extend(self._drain_pending_inbound())
        blocked = self._blocked_numbers()
        # Anything left in `sending` from a previous process is of unknown outcome: never resend automatically.
        for m in rows(self.conn, "SELECT * FROM messages WHERE direction='outbound' AND status='sending'"):
            self.conn.execute("UPDATE messages SET status='ambiguous', last_error='crash between provider call and record' WHERE id=?", (m["id"],))
            conv = self._conv(m["conversation_id"])
            if not row(self.conn, "SELECT 1 FROM escalations WHERE conversation_id=? AND reason='sms_ambiguous' AND status='open'", (conv["id"],)):
                self._escalate(conv, "sms_ambiguous", "Message #%d (%s) was in flight when the process stopped; confirm with provider before any resend"
                               % (m["id"], m["template_id"]), None)
            out.append("ambiguous:%d" % m["id"])
        self.conn.commit()
        pause = self.paused()
        quiet = in_quiet_hours(now, self.policy)
        for mid in [r["id"] for r in rows(self.conn, "SELECT id FROM messages WHERE direction='outbound' AND status='queued' ORDER BY id")]:
            # Re-read the message AND its conversation immediately before each provider call: an earlier
            # failure in this same batch may have changed ownership (epoch) or state.
            m = row(self.conn, "SELECT m.*, c.state conv_state, c.epoch conv_epoch, c.patient_id FROM messages m "
                               "JOIN conversations c ON c.id=m.conversation_id WHERE m.id=?", (mid,))
            if not m or m["status"] != "queued":
                continue
            kind = m["kind"]
            if kind not in COMPLIANCE_KINDS:
                patient = row(self.conn, "SELECT * FROM patients WHERE id=?", (m["patient_id"],))
                why = self._contact_allowed(patient)
                if why:
                    self.conn.execute("UPDATE messages SET status='suppressed', last_error=? WHERE id=?", (why, m["id"]))
                    out.append("suppressed:%d:%s" % (m["id"], why))
                    continue
                if m["to_phone"] in blocked:
                    out.append("held_pending_stop:%d" % m["id"])
                    continue
                if m["to_phone"] != patient["phone"]:
                    self.conn.execute("UPDATE messages SET status='cancelled', last_error='recipient phone changed since queueing' WHERE id=?", (m["id"],))
                    log_event(self.conn, now.strftime(ISO), "system", "queued_message_cancelled", conversation_id=m["conversation_id"],
                              detail={"message_id": m["id"], "reason": "recipient changed"})
                    out.append("cancelled:%d:recipient" % m["id"])
                    continue
                invalid = []
                if m["content_deps"]:
                    deps = json.loads(m["content_deps"])
                    for sid, ver in deps.get("sites", {}).items():
                        if self.directory.site(sid, now) is None or self.directory.site_meta(sid) != ver:
                            invalid.append(sid)
                    for key, appr in deps.get("instructions", {}).items():
                        if self.directory.instruction(key, now) is None or self.directory.instruction_meta(key) != appr:
                            invalid.append("instruction:%s" % key)
                elif m["site_ids"]:
                    invalid = [sid for sid in json.loads(m["site_ids"]) if self.directory.site(sid, now) is None]
                if invalid:
                    self.conn.execute("UPDATE messages SET status='cancelled', last_error='directory entry no longer valid', dedupe_key=dedupe_key||':cancelled:'||id WHERE id=?", (m["id"],))
                    if m["template_id"] == "outreach_initial" and kind == "scheduled":
                        # the patient never saw a first text: the next pass must send a FIRST text again, not a follow-up
                        self.conn.execute("UPDATE conversations SET outreach_attempts=MAX(0, outreach_attempts-1), opener_variant=NULL WHERE id=?", (m["conversation_id"],))
                    log_event(self.conn, now.strftime(ISO), "system", "queued_message_cancelled", conversation_id=m["conversation_id"],
                              detail={"message_id": m["id"], "reason": "directory invalid", "site_ids": invalid})
                    if m["conv_state"] not in ("closed",) and m["template_id"] in ("reminder", "plan_confirmed"):
                        self._escalate(self._conv(m["conversation_id"]), "directory_empty",
                                       "Agreed site %s is no longer verified; plan needs a human" % invalid, None)
                    elif m["conv_state"] in ("engaged", "outreach_sent", "new"):
                        # the patient was owed a reply; make sure the cadence produces one from current data
                        self._set_next(m["conversation_id"], "followup", now)
                    out.append("cancelled:%d:directory" % m["id"])
                    continue
                if m["epoch"] != m["conv_epoch"] or m["conv_state"] == "closed":
                    self.conn.execute("UPDATE messages SET status='cancelled', last_error='conversation state changed after queueing' WHERE id=?", (m["id"],))
                    log_event(self.conn, now.strftime(ISO), "system", "queued_message_cancelled", conversation_id=m["conversation_id"],
                              detail={"message_id": m["id"], "template": m["template_id"], "reason": "stale epoch or closed"})
                    out.append("cancelled:%d" % m["id"])
                    continue
                if pause and kind not in SAFETY_KINDS and not (pause == _feed.PAUSE_REASON and m["template_id"] == "hold_ack"):
                    continue                      # Sept 23 (V3): the AUTOMATIC integrity pause lets the hold acknowledgement out; a person's pause stops everything but safety
                if quiet and kind == "scheduled":
                    continue
                if self._feed_gate(patient, kind, m["template_id"]):
                    continue                      # Sept 23: stale feed / integrity block / per-patient hold, decided in ONE place for every non-safety message
            elif m["conv_state"] == "closed" and m["template_id"] == "help":
                self.conn.execute("UPDATE messages SET status='suppressed' WHERE id=?", (m["id"],))
                continue
            if not m["to_phone"]:
                self.conn.execute("UPDATE messages SET status='failed', last_error='no phone' WHERE id=?", (m["id"],))
                continue
            # --- commit-before-call ---
            self.conn.execute("UPDATE messages SET status='sending', attempts=attempts+1 WHERE id=?", (m["id"],))
            self.conn.commit()
            try:
                res = self.messaging.send(m["to_phone"], m["body"], m["dedupe_key"])
            except AmbiguousSendError as e:
                self.conn.execute("UPDATE messages SET status='ambiguous', last_error=? WHERE id=?", (str(e), m["id"]))
                conv = self._conv(m["conversation_id"])
                if not row(self.conn, "SELECT 1 FROM escalations WHERE conversation_id=? AND reason='sms_ambiguous' AND status='open'", (conv["id"],)):
                    self._escalate(conv, "sms_ambiguous", "Message #%d (%s): provider outcome unknown (%s); confirm before any resend"
                                   % (m["id"], m["template_id"], e), m["id"])
                log_event(self.conn, now.strftime(ISO), "system", "outbound_ambiguous", conversation_id=m["conversation_id"],
                          detail={"message_id": m["id"], "error": str(e)})
                self.conn.commit()
                out.append("ambiguous:%d" % m["id"])
                continue
            except MessagingError as e:
                attempts = m["attempts"] + 1
                if attempts >= self.policy.sms_max_attempts:
                    self.conn.execute("UPDATE messages SET status='failed', last_error=? WHERE id=?", (str(e), m["id"]))
                    conv = self._conv(m["conversation_id"])
                    if conv["state"] not in ("closed", "escalated"):
                        self._escalate(conv, "sms_delivery_failed", "Outbound failed %d times: %s" % (attempts, e), m["id"])
                    out.append("failed:%d" % m["id"])
                else:
                    self.conn.execute("UPDATE messages SET status='queued', last_error=? WHERE id=?", (str(e), m["id"]))
                    out.append("retry_later:%d" % m["id"])
                log_event(self.conn, now.strftime(ISO), "system", "outbound_send_error", conversation_id=m["conversation_id"],
                          detail={"message_id": m["id"], "attempt": attempts, "error": str(e)})
                self.conn.commit()
                continue
            # any non-MessagingError exception propagates and leaves the row in `sending` (ambiguous) by design
            self.conn.execute("UPDATE messages SET status='sent', provider=?, provider_message_id=?, sent_at=? WHERE id=?",
                              (res.provider, res.provider_message_id, now.strftime(ISO), m["id"]))
            self.conn.execute("UPDATE conversations SET outbound_count=outbound_count+1 WHERE id=?", (m["conversation_id"],))
            log_event(self.conn, now.strftime(ISO), "system", "outbound_sent", conversation_id=m["conversation_id"],
                      detail={"message_id": m["id"], "provider": res.provider, "simulated": res.simulated,
                              "segments": m["segments"], "template": m["template_id"], "kind": kind,
                              "note": "provider accepted; delivery not modeled"})
            self.conn.commit()
            out.append("sent:%d:%s" % (m["id"], m["template_id"]))
        return out


def _short_time(hhmm: str) -> str:
    h, m = hhmm.split(":")
    h = int(h)
    return "%d%s%s" % (h % 12 or 12, (":" + m) if m != "00" else "", "am" if h < 12 else "pm")
