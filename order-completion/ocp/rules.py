"""Explicit software rules.  The model never decides these; code does.

Everything a conversation is *allowed* to do is enumerated here: which order
transitions are legal, when outreach may be sent, how many attempts, per-conversation
usage ceilings, quiet hours, and how untrusted patient text is pre-screened before
any model sees it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

from .models import ORDER_TRANSITIONS, OPT_OUT_KEYWORDS, HELP_KEYWORDS, INTENTS


OVERDUE_THRESHOLD_MIN, OVERDUE_THRESHOLD_MAX = 15, 45


@dataclass
class Policy:
    """Tunable limits.  Defaults are deliberately conservative."""
    min_order_age_days: int = 45              # overdue threshold; configurable 15..45 (see Engine.set_overdue_threshold)
    reduced_followup_interval_days: int = 7   # cadence when the patient asked for fewer reminders
    reduced_max_outreach_attempts: int = 2
    max_outreach_attempts: int = 4            # initial + 3 follow-ups
    followup_interval_days: int = 3
    reminder_lead_hours: int = 20             # reminder before an agreed plan
    plan_verification_grace_days: int = 5     # after agreed date, wait this long for partner feed
    quiet_hours_start: time = time(20, 0)     # no sends 8pm–8am local
    quiet_hours_end: time = time(8, 0)
    clinician_hours_start: time = time(8, 0)  # partner clinician business hours
    clinician_hours_end: time = time(17, 0)
    clinician_weekdays_only: bool = True
    stale_feed_hours: int = 48                # pause outreach if newest feed older than this
    max_model_calls_per_conversation: int = 20
    max_tokens_per_conversation: int = 60_000
    max_outbound_per_conversation: int = 12
    max_inbound_per_conversation: int = 40
    max_inbound_chars: int = 1000             # truncate before model
    model_confidence_floor: float = 0.6
    model_max_retries: int = 2
    sms_max_attempts: int = 3
    max_message_segments: int = 3             # never send a 4-segment SMS
    max_spend_usd_per_day: float = 5.0        # application-level brake on live model spend (rolling 24h, wall clock)
    composer_retries: int = 1                 # extra attempts a live composer gets after a fact-check refusal
    opener_sites: int = 2                     # sites named in the first text (2 or 3)
    opener_disclosure: str = "ab"             # none | short | ab (alternate by conversation id; partner-negotiated, A/B tested)
    # --- version 4 -------------------------------------------------------------------------------------------
    clinical_handoff: str = "portal_link"     # portal_link | portal_relay | clinician_queue (models.CLINICAL_HANDOFF_MODES)
    clinical_pause_days: int = 7              # after a portal referral: quiet days before one gentle follow-up
    resolver_mode: str = "on"                 # off | shadow (record only) | on: the resolver tries before a Kate item opens
    resolver_reviewer: str = "rules"          # rules | claude | openai: who checks the resolver's choice
    planner_shadow: bool = True               # record the classifier's proposed_action next to the rule-chosen action
    out_of_area_miles: float = 25.0           # no capable site within this distance of the patient's location => out of area
    location_link_enabled: bool = True        # offer the consent-based location-share page when the patient is not near home
    location_link_ttl_hours: int = 48         # single-use link lifetime
    location_ttl_days: int = 30               # a stored patient location expires (data minimization)
    emergency_notify_partner: bool = True     # open an emergency-priority partner item in addition to the 911 text
    unclear_menu: bool = True                 # second unclear reply gets a numbered menu before any person
    public_base_url: str = "http://127.0.0.1:8765"   # where the location-share page is served (production: the app's public HTTPS host)
    # --- version 5 -------------------------------------------------------------------------------------------
    booking_enabled: bool = True              # offer simulated booking at sites whose directory record has scheduling.adapter
    relay_followup_days: int = 5              # after a relay was sent: one follow-up asking whether the office answered
    operator_role: str = "operator"           # operator | clinical_reviewer (SIMULATED role switch for the demo)
    # --- feed integrity (Sept 23, 2026) — never outsource to the partner's technology team what we can check ourselves
    feed_expected_every_hours: int = 24       # a partner file is expected this often
    feed_late_grace_hours: int = 6            # late alert to the partner's technical contact after expected + grace (before the stale pause)
    feed_results_silent_days: int = 7         # orders keep arriving but no result/cancellation event for this long → alert
    feed_bad_rows_max_fraction: float = 0.10  # more than this share of rows failing row checks (min 3) → whole file held
    feed_rekey_fraction: float = 0.20         # more than this share of DISTINCT known patients arriving with both a new name and a new phone → file held
    feed_rekey_min_patients: int = 5          # the re-key check needs at least this many distinct known patients in the file
    feed_volume_drift_low: float = 0.25       # row count below this multiple of the partner's baseline → warning
    feed_volume_drift_high: float = 4.0       # row count above this multiple → warning
    feed_baseline_min_files: int = 3          # accepted files before drift / silence checks apply


class RuleViolation(Exception):
    pass


def check_transition(current: str, new: str) -> None:
    if new == current:
        return
    allowed = ORDER_TRANSITIONS.get(current, set())
    if new not in allowed:
        raise RuleViolation("order transition %s -> %s not allowed" % (current, new))


def order_is_eligible(order: Dict, patient: Dict, now: datetime, policy: Policy) -> Tuple[bool, str]:
    """Overdue = ordered at least `min_order_age_days` ago AND (no intended due date, or the intended due date
    has passed).  An order awaiting a future intended date is never overdue, whatever its age."""
    ordered = datetime.fromisoformat(order["ordered_at"])
    age = (now - ordered).days
    if age < policy.min_order_age_days:
        return False, "order age %d days < %d-day threshold" % (age, policy.min_order_age_days)
    due = order.get("intended_due_at")
    if due:
        try:
            if datetime.fromisoformat(due) > now:
                return False, "intended due date %s is in the future" % due[:10]
        except ValueError:
            return False, "intended_due_at unparseable"
    if order.get("priority") not in (None, "routine"):
        return False, "non-routine priority (%s) excluded from automated outreach" % order.get("priority")
    if not patient.get("phone"):
        return False, "no phone on file"
    if not patient.get("consent_sms"):
        return False, "no SMS contact consent recorded by partner"
    return True, "eligible"


def in_quiet_hours(now: datetime, policy: Policy) -> bool:
    t = now.time()
    if policy.quiet_hours_start > policy.quiet_hours_end:   # wraps midnight
        return t >= policy.quiet_hours_start or t < policy.quiet_hours_end
    return policy.quiet_hours_start <= t < policy.quiet_hours_end


def next_send_window(now: datetime, policy: Policy) -> datetime:
    """Earliest time a message may go out.  Returns `now` when not in quiet hours."""
    if not in_quiet_hours(now, policy):
        return now
    candidate = now.replace(hour=policy.quiet_hours_end.hour, minute=policy.quiet_hours_end.minute,
                            second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def clinician_available(now: datetime, policy: Policy) -> bool:
    if policy.clinician_weekdays_only and now.weekday() >= 5:
        return False
    return policy.clinician_hours_start <= now.time() < policy.clinician_hours_end


_HHMM_RE = re.compile(r"^\d{1,2}:\d{2}$")
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def time_bounds_contradict(constraints: Dict) -> bool:
    """after_time >= before_time is an empty window (e.g. 'after 3pm before 9am')."""
    a, b = constraints.get("after_time"), constraints.get("before_time")
    return bool(a and b and a >= b)


def add_business_hours(start: datetime, hours: float, policy: Policy) -> datetime:
    """Advance `start` by `hours` counted only inside clinician windows (Mon-Fri 08-17 by default)."""
    remaining = timedelta(hours=hours)
    t = start
    for _ in range(60):   # bounded: at most ~2 months of calendar scanning
        if not clinician_available(t, policy):
            # jump to the next window start
            candidate = t.replace(hour=policy.clinician_hours_start.hour, minute=policy.clinician_hours_start.minute,
                                  second=0, microsecond=0)
            if candidate <= t:
                candidate += timedelta(days=1)
            while not clinician_available(candidate, policy):
                candidate += timedelta(days=1)
            t = candidate
        window_end = t.replace(hour=policy.clinician_hours_end.hour, minute=policy.clinician_hours_end.minute,
                               second=0, microsecond=0)
        if t + remaining <= window_end:
            return t + remaining
        remaining -= (window_end - t)
        t = window_end
    return t


# --- Pre-screen of untrusted patient text --------------------------------------
_OPT_OUT_RE = re.compile(r"^\W*(%s)\W*$" % "|".join(sorted(OPT_OUT_KEYWORDS)), re.IGNORECASE)
_HELP_RE = re.compile(r"^\W*(%s)\W*$" % "|".join(sorted(HELP_KEYWORDS)), re.IGNORECASE)
_WRONG_NUMBER_RE = re.compile(r"\b(wrong (number|person)|not (him|her|them|that person)|who is this\??|"
                              r"you have the wrong)\b", re.IGNORECASE)
# Version 4: emergency wording is decided by CODE before any model.  Deliberately broad: a false positive costs one
# safety text with the 911 line; a miss could cost far more.
EMERGENCY_RE = re.compile(r"\b(chest pains?|chest (is )?tight\w*|can'?t (breathe|catch my breath|get (my|a) breath)|cannot breathe|trouble breathing|"
                          r"struggling to breathe|short(ness)? of breath|bleeding (heavily|a lot|won'?t stop|that won'?t stop)|heavy bleeding|"
                          r"(vomiting|coughing( up)?|throwing up) blood|overdos\w*|suicid\w*|kill (myself|me)|end my life|want to die|"
                          r"don'?t want to (live|be alive)|took too many (pills|of my)|took (all|a bottle of) (my|the) pills|unconscious|passed out|"
                          r"fainted|collapsed|not (breathing|waking up|responding)|stroke|heart attack|seizure|convuls\w*|"
                          r"face (is )?droop\w*|speech (is )?slurred|slurred speech|numb (on|down) (one|my (left|right)) side|"
                          r"severe (chest|abdominal|stomach|head) pain|worst headache|severe allergic|allergic reaction|anaphyla\w*|"
                          r"(throat|tongue|lips?) (is |are )?swell\w*|can'?t swallow|call(ed|ing)? 911|\b911\b|emergency)\b", re.IGNORECASE)
# Figurative uses that must NOT trip the screen ("this bill is giving me chest pain", "what a headache").  Governed phrase
# set and recall measurement are a partner/clinical item (Codex V4-5); this list is the prototype's floor, not a claim.
EMERGENCY_IDIOM_RE = re.compile(r"\b(giv(e|es|ing) me (a )?(chest pain|heart attack|stroke|headache)|(is|was) (a|such a) (pain|headache|nightmare)|"
                                r"(bill|cost|price|paperwork|form|traffic|parking)[^.?!]{0,30}(chest pain|heart attack|stroke)|"
                                r"not an? emergency|no emergency|isn'?t an? emergency|in case of emergency|emergency contact)\b", re.IGNORECASE)
def emergency_signal(text: str) -> bool:
    """Code floor for emergency wording (V5-5): figurative phrases are removed from the text first, then any REMAINING
    emergency signal counts.  "My emergency contact says I took too many pills" → "emergency contact" removed → "took too
    many pills" still fires.  "This bill is giving me chest pain" → the idiom is removed → nothing left → no alarm."""
    stripped = EMERGENCY_IDIOM_RE.sub(" ", text or "")
    return bool(EMERGENCY_RE.search(stripped))


# The patient said a constraint is absolute ("only", "must", "can't do any other").  Such constraints are never relaxed by the resolver.
ABSOLUTE_RE = re.compile(r"\b(only|the only|must|have to|has to|cannot do|can'?t do (any|another|other)|no other|nothing else|not (any )?other|"
                         r"strictly|absolutely|is the only|are the only)\b", re.IGNORECASE)
_MENU_RE = re.compile(r"^\W*([123])\W*$")


@dataclass
class PreScreen:
    text: str
    truncated: bool = False
    hard_intent: Optional[str] = None      # set when code decides without the model
    flags: List[str] = field(default_factory=list)
    menu_digit: Optional[str] = None       # "1" | "2" | "3" when the text is a bare menu digit (meaningful only after a menu text)


def prescreen_inbound(raw: str, policy: Policy) -> PreScreen:
    """Deterministic checks that must win over anything a model says."""
    text = (raw or "").strip()
    ps = PreScreen(text=text)
    if len(text) > policy.max_inbound_chars:
        ps.text = text[: policy.max_inbound_chars]
        ps.truncated = True
        ps.flags.append("truncated")
    if _OPT_OUT_RE.match(text):
        ps.hard_intent = "opt_out"
    elif _HELP_RE.match(text):
        ps.hard_intent = "help"
    elif _WRONG_NUMBER_RE.search(text):
        ps.hard_intent = "wrong_number"
    elif emergency_signal(text):
        ps.hard_intent = "emergency"
    if ABSOLUTE_RE.search(text):
        ps.flags.append("absolute_wording")
    m = _MENU_RE.match(text)
    if m:
        ps.menu_digit = m.group(1)
    lowered = text.lower()
    if any(k in lowered for k in ("ignore previous", "ignore all", "system prompt", "you are now",
                                  "disregard", "as an ai", "reveal", "print your instructions")):
        ps.flags.append("instruction_like_text")
    return ps


def validate_model_output(out: Dict) -> Dict:
    """Reject anything outside the closed vocabulary.  Never trust free text."""
    intent = out.get("intent")
    if intent not in INTENTS:
        intent = "unclear"
    try:
        conf = float(out.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    barrier = out.get("barrier") if out.get("barrier") in (
        "none", "schedule", "transport", "cost", "clinical", "identity", "language", "other") else "none"
    constraints = out.get("constraints") or {}
    clean: Dict = {}
    if isinstance(constraints, dict):
        for k in ("after_time", "before_time"):
            v = constraints.get(k)
            if isinstance(v, str) and _HHMM_RE.match(v.strip()):
                h, m = v.strip().split(":")
                if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
                    clean[k] = "%02d:%02d" % (int(h), int(m))
        v = constraints.get("weekday")
        if isinstance(v, str) and v.strip().lower()[:3] in WEEKDAYS:
            clean["weekday"] = v.strip().lower()[:3]
        for k in ("weekend_ok", "evening_ok"):
            if isinstance(constraints.get(k), bool):
                clean[k] = constraints[k]
        v = constraints.get("site_choice")
        if isinstance(v, (str, int)) and str(v).strip() in ("1", "2"):
            clean["site_choice"] = str(v).strip()
        for k in ("caregiver", "in_network", "corrects"):
            if isinstance(constraints.get(k), bool):
                clean[k] = constraints[k]
        for k in ("town", "where"):
            v = constraints.get(k)
            if isinstance(v, str) and v.strip():
                clean[k] = re.sub(r"[^A-Za-z0-9 .'-]", "", v.strip())[:40]
        v = constraints.get("reminder_frequency")
        if v in ("reduced", "normal"):
            clean["reminder_frequency"] = v
        v = constraints.get("zip")
        if isinstance(v, (str, int)) and re.match(r"^\d{5}$", str(v).strip()):
            clean["zip"] = str(v).strip()
        v = constraints.get("topic")
        if v in CLINICAL_TOPICS:
            clean["topic"] = v
        if isinstance(constraints.get("absolute"), bool):
            clean["absolute"] = constraints["absolute"]
    proposed = out.get("proposed_action")
    proposed = proposed if isinstance(proposed, str) and re.match(r"^[a-z_]{3,40}$", proposed) else None
    return {"intent": intent, "confidence": conf, "barrier": barrier, "constraints": clean, "proposed_action": proposed}


# Sub-topics of a clinical question.  Only "prep" can be answered by the application, and only from partner-approved
# preparation text in the service catalog; every other topic goes through the clinical handoff.
CLINICAL_TOPICS = ("prep", "needed", "rationale", "results", "symptoms", "medication", "emergency", "other")


def usage_exceeded(conv: Dict, policy: Policy, queued_outbound: int = 0) -> Optional[str]:
    """`outbound_count` is provider-accepted sends; queued/in-flight messages are reserved too."""
    if conv["model_calls"] >= policy.max_model_calls_per_conversation:
        return "model_calls"
    if conv["input_tokens"] + conv["output_tokens"] >= policy.max_tokens_per_conversation:
        return "tokens"
    if conv["outbound_count"] + queued_outbound >= policy.max_outbound_per_conversation:
        return "outbound_messages"
    if conv["inbound_count"] >= policy.max_inbound_per_conversation:
        return "inbound_messages"
    return None


def model_attempts_allowed(conv: Dict, policy: Policy) -> int:
    """How many provider attempts this conversation may still spend (shared engine/router budget)."""
    remaining = policy.max_model_calls_per_conversation - conv["model_calls"]
    return max(0, min(policy.model_max_retries + 1, remaining))
