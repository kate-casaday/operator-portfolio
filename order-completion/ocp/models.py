"""Domain enums and constants.  Python 3.9 compatible (no match, no X | Y)."""
from __future__ import annotations

# --- Order states (per order) -------------------------------------------------
ORDER_STATES = [
    "imported",            # loaded from partner feed, not yet screened
    "eligible",            # passed eligibility rules; outreach may start
    "ineligible",          # failed rules (e.g., < 45 days, no consent, no phone)
    "outreach_active",     # conversation in progress for this order
    "claimed_complete",    # patient says done; awaiting partner reconciliation
    "verified_complete",   # partner feed shows result finalized for this order
    "cancelled_by_partner",
    "suppressed",          # opt-out / wrong number / partner hold
    "unresolved",          # max attempts or barrier could not be resolved
    "escalated",           # in human queue
    "completed_external",  # completed OUTSIDE the partner's lab network; set only by a human with an evidence note
    "replaced",            # superseded by a newer provider order (v5); terminal for this order, the replacement carries on
]

ORDER_TRANSITIONS = {
    "imported": {"eligible", "ineligible", "cancelled_by_partner", "verified_complete", "suppressed", "replaced"},
    "eligible": {"outreach_active", "cancelled_by_partner", "verified_complete", "suppressed", "ineligible",
                 "claimed_complete", "escalated", "replaced"},
    "outreach_active": {"claimed_complete", "verified_complete", "cancelled_by_partner", "suppressed",
                        "unresolved", "escalated", "replaced"},
    "claimed_complete": {"verified_complete", "outreach_active", "cancelled_by_partner", "suppressed",
                         "escalated", "unresolved", "completed_external", "replaced"},
    "escalated": {"outreach_active", "verified_complete", "cancelled_by_partner", "suppressed", "unresolved",
                  "claimed_complete", "completed_external", "replaced"},
    "completed_external": set(),
    "unresolved": {"verified_complete", "cancelled_by_partner", "suppressed", "outreach_active", "replaced"},
    "verified_complete": set(),
    "cancelled_by_partner": set(),
    "replaced": set(),
    "suppressed": {"verified_complete", "cancelled_by_partner", "replaced"},
    "ineligible": {"eligible", "cancelled_by_partner", "verified_complete", "suppressed", "replaced"},
}

# --- Conversation states (per patient) ----------------------------------------
CONV_STATES = [
    "new",             # created, nothing sent
    "outreach_sent",   # first message queued/sent, no reply yet
    "engaged",         # patient replied at least once
    "plan_agreed",     # patient chose a site/time; reminder scheduled
    "waiting_partner", # claimed complete or clinical hold; waiting on partner
    "paused",          # paused by Kate or by policy (stale feed, usage limit)
    "escalated",       # human queue owns next step
    "closed",          # opt-out / wrong number / all orders terminal
]

# --- Intents the model may return (closed set; anything else is rejected) ----
INTENTS = [
    "willing",            # ready to go / asks how
    "needs_location",     # where do I go
    "needs_hours",        # when are you open
    "scheduling_barrier", # works late, needs evening/weekend, specific day
    "transport_barrier",  # cannot get there
    "cost_question",      # how much / insurance
    "clinical_question",  # do I still need this / what is it for / symptoms
    "already_completed",  # says they already did it
    "opt_out",            # stop / unsubscribe (also rule-detected)
    "wrong_number",       # not this person
    "confirm_plan",       # yes that works / I'll go Friday
    "reschedule",         # can't make it, later
    "request_clinical_staff",  # wants to talk to a nurse / doctor / the care team before or instead of acting
    "request_human",      # wants a real person (non-clinical coordinator)
    "fewer_reminders",    # asks for less frequent contact (not opt-out)
    "correction",         # corrects something the system said or assumed (day, site, name); constraints carry the fix
    "unclear",            # cannot classify
    "abusive_or_off_topic",
    "emergency",          # emergency wording (also code-detected before any model): 911 / urgent care text, hold
    "plan_changed_report",  # v5: the patient says the provider changed or dropped the plan; pause + reconcile, never change the target
]

BARRIERS = ["none", "schedule", "transport", "cost", "clinical", "identity", "language", "other"]

# Preference keys that may be persisted from validated constraints.  Source is always recorded.
PREFERENCE_KEYS = ("after_time", "before_time", "weekday", "weekend_ok", "evening_ok", "town", "caregiver",
                   "reminder_frequency", "preferred_site", "hard_constraints")   # hard_constraints: JSON list of keys the patient called absolute (v4)
PREFERENCE_SOURCES = ("patient_statement", "model_interpretation", "kate", "partner_feed")

# --- Escalation reasons --------------------------------------------------------
ESCALATION_REASONS = {
    "clinical_question": "Clinical question — route to partner clinician",
    "cost_question": "Cost / insurance question — needs verified estimate",
    "already_completed_claim": "Patient says already done — reconcile with partner",
    "unresolved_barrier": "Barrier not resolvable with approved options",
    "identity_uncertain": "Identity or wrong-number uncertainty",
    "model_low_confidence": "Model could not classify with confidence",
    "provider_failure": "Model/messaging provider failed after retries",
    "usage_limit": "Per-conversation usage limit reached",
    "abusive_or_off_topic": "Abusive or off-topic message",
    "sms_delivery_failed": "Outbound SMS failed after retries",
    "sms_ambiguous": "Outbound SMS in unknown state after a crash — reconcile with provider before any resend",
    "processing_error": "Inbound could not be processed (software error) — patient got a generic handoff",
    "directory_empty": "No verified site available to offer",
    "clinician_overdue": "Clinician queue item past its response deadline",
    "clinical_staff_request": "Patient asked to speak with clinical staff — route to partner clinician",
    "human_request": "Patient asked for a real person — coordinator follow-up",
    "spend_cap": "Daily model-spend cap reached — automation paused for this reply; a person answers",
    "unmapped_order": "Order carries a test code the service catalog does not map — a person confirms which site can perform it",
    "emergency_wording": "Emergency wording in a patient text — 911 / urgent-care text sent; partner FYI (version 4)",
    "out_of_area": "Patient is outside the partner's service area — nearest site and distance attached (version 4)",
    "resolver_disagreement": "Resolver and reviewer disagreed on how to solve an operational dead end — both rationales attached (version 4)",
    "clinical_route_missing": "No clinical route is configured (no approved portal link, and the partner has not opted into a clinician queue) — the patient was given the clinic phone only (version 4)",
    "portal_ambiguous": "Portal relay in unknown state after a crash — reconcile with the partner before any resend (version 4)",
    "plan_change_reconcile": "Patient reports the provider changed or dropped the plan — outreach paused; reconcile against the partner's order events (version 5)",
    "emergency_followup": "Next-day follow-up after emergency wording — confirm partner acknowledgement and patient status through the partner (version 5)",
    "booking_change_failed": "The scheduling adapter refused to cancel or move a booking — the booking still stands; a person reconciles with the partner (version 5)",
    "capability_denied": "An action was refused because the partner has not granted that capability (messaging, booking, relay, record read) (version 5)",
}

# Handoff status of an escalation.  'queued_simulated' = a row in this database and nothing else;
# 'notified' would require a real integration (none exists); 'accepted' = the assignee acknowledged it.
HANDOFF_STATUSES = ("queued_simulated", "notified", "accepted")

# Escalations that go to the partner clinician queue rather than Kate.
CLINICIAN_QUEUE_REASONS = {"clinical_question", "clinical_staff_request", "emergency_wording"}

# Version 4: how a clinical question or a request for clinical staff reaches the partner.
#   portal_link     tell the patient it is a care-team question and give the partner's portal link + phone (default)
#   portal_relay    offer to send the patient's OWN words to the provider's office through the portal (simulated adapter)
#   clinician_queue version 3: a row in our clinician queue that the partner must staff
CLINICAL_HANDOFF_MODES = ("portal_link", "portal_relay", "clinician_queue")
RESOLVER_MODES = ("off", "shadow", "on")
RESOLVER_REVIEWERS = ("rules", "claude", "openai")
ESCALATION_PRIORITIES = ("normal", "emergency")

# Human-minute defaults per escalation reason (assumed; editable at resolution time).
DEFAULT_HUMAN_MINUTES = {
    "clinical_question": 0.0,   # partner clinician's time, tracked separately
    "cost_question": 5.0,
    "already_completed_claim": 4.0,
    "unresolved_barrier": 8.0,
    "identity_uncertain": 3.0,
    "model_low_confidence": 3.0,
    "provider_failure": 2.0,
    "usage_limit": 3.0,
    "abusive_or_off_topic": 2.0,
    "sms_delivery_failed": 2.0,
    "sms_ambiguous": 3.0,
    "processing_error": 3.0,
    "directory_empty": 3.0,
    "clinician_overdue": 1.0,
    "clinical_staff_request": 0.0,
    "human_request": 6.0,
    "spend_cap": 3.0,
    "unmapped_order": 5.0,
    "emergency_wording": 0.0,   # partner clinician's time
    "out_of_area": 5.0,
    "resolver_disagreement": 4.0,
    "clinical_route_missing": 2.0,
    "portal_ambiguous": 3.0,
    "plan_change_reconcile": 5.0,
    "emergency_followup": 4.0,
    "booking_change_failed": 4.0,
    "capability_denied": 2.0,
}

# Conversation states in which automation does not act on ordinary replies (a human owns the next step).
HELD_STATES = {"waiting_partner", "escalated", "paused"}
HOLD_REASONS_CLINICAL = {"clinical_question", "clinical_staff_request", "already_completed_claim", "emergency_wording", "plan_change_reconcile"}

# Outbound message kinds.  Only these two may leave during a Kate pause; only these three during quiet hours.
COMPLIANCE_KINDS = {"compliance"}           # STOP / wrong-number / HELP confirmations
SAFETY_KINDS = {"compliance", "safety"}     # + clinical acknowledgements with the clinic phone / 911 language
REPLY_KINDS = {"compliance", "safety", "reply"}   # anything answering a message the patient just sent
MAX_HELP_REPLIES_PER_CONVERSATION = 3

OPT_OUT_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
HELP_KEYWORDS = {"help", "info"}
