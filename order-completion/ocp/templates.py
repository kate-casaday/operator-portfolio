"""Approved outbound templates (v0.3).  The model chooses *which* template through its intent
classification; it never writes the words that reach a patient.  Placeholders fill only from the verified
directory and the partner feed.  Visit requirements are site data, not template prose.

Voice rules: concise, warm, transparently automated (the first text says so; acks for human/clinical
requests say so again), never implies a booking was made or an appointment slot exists, never quotes a
price, never states clinical facts.

Where templates constrain the experience (and whether constrained generation would help) is documented in
docs/technical-brief-2026-09-15-decision-history.md § "Templates versus constrained generation".
"""
from __future__ import annotations

import re
from typing import Dict, Set

TEMPLATES: Dict[str, str] = {
    # --- outreach cadence -------------------------------------------------------------------------
    "outreach_initial": (
        "{partner_name}: Hi {first_name}, I'm from {provider}'s office about lab testing ordered for you. It can be "
        "completed by walk-in or appointment at a {partner_name} lab near you. Reply with a day that could work, or "
        "any question, and I'll send the options. Reply STOP to opt out."),
    "outreach_followup": (
        "{partner_name}: Hi {first_name}, I'm from {provider}'s office, following up on your lab testing. Would a weekday "
        "or a weekend work better? A quick reply is all I need. Reply STOP to opt out."),
    "outreach_followup_reduced": (
        "{partner_name}: Hi {first_name}, I'm from {provider}'s office with the one reminder you asked for about your lab "
        "testing. Reply with a day that could work, or STOP to end texts."),
    # --- locations and plans -----------------------------------------------------------------------
    "offer_sites": (
        "{partner_name}: Two options: {site_1}. {site_2}.{link_line} Which works for you? If neither does, tell us what's in the way."),
    "offer_sites_constrained": (
        "{partner_name}: Open when you said you're free: {site_1}. {site_2}.{link_line} Which works for you?"),
    "offer_sites_nearby": (
        "{partner_name}: Closest to {town}: {site_1}. {site_2}.{link_line} Which works for you?"),
    "no_capable_site": (
        "{partner_name}: I need to check which location can do this particular test. A coordinator (a real person) will "
        "text you within one business day with the right place."),
    "no_site_matches": (
        "{partner_name}: We don't have a location open at that time yet. A coordinator (a real person) will look at "
        "other options and text you back within one business day."),
    "ask_day": (
        "{partner_name}: Got it, {site_name}. Which day will you go? Reply with a day of the week."),
    "plan_confirmed": (
        "{partner_name}: Got it - {when} at {site_name}. We'll text a reminder the day before. Walk-in plan, not a "
        "booked appointment; prep instructions come from your care team. Reply STOP to opt out."),
    "reminder": (
        "{partner_name}: Reminder: you planned to visit {site_name}, {site_address} {when}. Walk in during the listed hours."),
    "clarify_times": (
        "{partner_name}: Just to be sure - do you need a time after {after} or before {before}? Reply with one, and a day that works."),
    "reschedule_ack": (
        "{partner_name}: No problem. What day or time would work better?"),
    # --- completion claims ---------------------------------------------------------------------------
    "already_completed_ack": (
        "{partner_name}: Good to know, thank you. Was that at a {partner_name} lab or somewhere else? We'll check the "
        "records and only text again if we need something."),
    "completed_in_network_ack": (
        "{partner_name}: Thanks - we'll look for your result in {partner_name}'s records. No more texts unless we need something."),
    "completed_out_of_network_ack": (
        "{partner_name}: Thanks - we'll ask your care team to request those results from there. No more texts unless we need something."),
    # --- barriers -------------------------------------------------------------------------------------
    "cost_ack": (
        "{partner_name}: Good question. We can't quote a price by text. A coordinator will check with {partner_name} "
        "billing and text you a verified answer within one business day."),
    "transport_ack": (
        "{partner_name}: Understood.{transport_instruction} A coordinator will also text you within one business day about other options."),
    "hold_ack": (
        "{partner_name}: Thanks. We're confirming your order with {partner_name} and will text you back."),
    # --- clinical and human requests (transparently automated) ----------------------------------
    "clinical_ack_business_hours": (
        "{partner_name}: That's a question for your care team. This text service is automated, so we've logged it for "
        "{clinician_name}; expect a reply during business hours today. If it's urgent, call {clinician_phone}."),
    "clinical_ack_after_hours": (
        "{partner_name}: That's a question for your care team. This text service is automated, so we've logged it for "
        "{clinician_name}; expect a reply on the next business day. If it's urgent, call {clinician_phone}. "
        "In an emergency, call 911."),
    "staff_ack_business_hours": (
        "{partner_name}: Of course. This text service is automated; we've logged your request for {clinician_name}, "
        "and someone there should reach you during business hours today. If it's urgent, call {clinician_phone}."),
    "staff_ack_after_hours": (
        "{partner_name}: Of course. This text service is automated; we've logged your request for {clinician_name}, "
        "and someone there should reach you on the next business day. If it's urgent, call {clinician_phone}. "
        "In an emergency, call 911."),
    "human_ack": (
        "{partner_name}: Of course - a coordinator (a real person) will text you within one business day. "
        "No more automated texts until then. If it's urgent, call {clinician_phone}."),
    # --- reminders preference ------------------------------------------------------------------------
    "fewer_reminders_ack": (
        "{partner_name}: Understood, we'll ease off. At most one more reminder in about a week, then we'll leave it "
        "with your care team. Reply STOP to end all texts."),
    # --- compliance ----------------------------------------------------------------------------------
    "opt_out_confirm": (
        "{partner_name}: You're unsubscribed and won't get more texts about this. Your care team still has your order on file."),
    "wrong_number_confirm": (
        "{partner_name}: Sorry for the mix-up. We've removed this number and won't text again."),
    "help": (
        "{partner_name}: This is an automated lab-testing reminder service for {partner_name} patients. Reply STOP to opt out. "
        "For questions call {clinician_phone}."),
    # --- version 4: clinical handoff through the partner's portal (approved wording; never composed) --------
    "clinical_portal_link": (
        "{partner_name}: That's a question for your care team, and this text service is automated. The fastest way to "
        "reach {provider}'s office is a message through {portal_name}: {portal_link} Or call {clinician_phone}. "
        "In an emergency, call 911."),
    "staff_portal_link": (
        "{partner_name}: Of course. This text service is automated, so the way to reach {clinician_name} is {clinician_phone}, "
        "or a message through {portal_name}: {portal_link} In an emergency, call 911."),
    "clinical_relay_offer": (
        "{partner_name}: That's a question for your care team, and this text service is automated. I can send it to "
        "{provider}'s office through {portal_name} as a message from you. If you'd like that, reply with the message "
        "exactly as you'd write it to them. Or message them yourself: {portal_link} In an emergency, call 911."),
    "clinical_relay_prompt": (
        "{partner_name}: Got it. Reply with the message itself, exactly as you'd write it to {provider}'s office, and I'll send it "
        "through {portal_name} as a message from you. Or reply NO and nothing is sent."),
    "clinical_call_instructions": (
        "{partner_name}: That's a question for your care team, and this text service is automated. Please call {clinician_phone}. "
        "In an emergency, call 911."),
    "clinical_relay_sent": (
        "{partner_name}: Sent to {provider}'s office through {portal_name}, in your words. They'll answer you there. "
        "If it's urgent, call {clinician_phone}."),
    "clinical_relay_cancelled": (
        "{partner_name}: OK, nothing was sent. You can reach {provider}'s office any time through {portal_name}: {portal_link} "
        "or by calling {clinician_phone}."),
    "clinical_followup_after_referral": (
        "{partner_name}: Hi {first_name}, I'm from {provider}'s office. If your care team said to go ahead with the "
        "{test_category}, reply with a day that could work and I'll send the closest place and hours. Reply STOP to opt out."),
    "prep_answer": (
        "{partner_name}: {prep_instruction} Anything beyond that is a question for your care team: {portal_link} "
        "If it's urgent, call {clinician_phone}."),
    "emergency_ack": (
        "{partner_name}: If this is an emergency, call 911 now.{urgent_care_line} This text service is automated and "
        "can't help in an emergency; I've flagged your message for {clinician_name}."),
    # --- version 5: documented rationale, plan changes, relay follow-up, booking (approved wording; never composed) ------
    "rationale_documented": (
        "{partner_name}: Here is what's written in your record. {rationale_author}'s note from {rationale_date} says: \"{rationale_excerpt}\" "
        "I can't add to that. Questions about it go to {provider}'s office: {portal_link}"),
    "rationale_unknown": (
        "{partner_name}: I don't see the reason written in what I have access to, so I won't guess. That's a question for {provider}'s office: "
        "{portal_link} Or call {clinician_phone}."),
    "plan_change_ack": (
        "{partner_name}: Thanks for telling me. I'll check with {provider}'s office about the change and won't send reminders about the "
        "{test_category} until that's confirmed. If they've already told you what to do, follow their instructions."),
    "plan_change_confirmed": (
        "{partner_name}: Hi {first_name}, {provider}'s office updated your order. Here's what's on file now: {order_summary}. "
        "Reply with a day that could work and I'll send the closest place and hours. Reply STOP to opt out."),
    "relay_followup": (
        "{partner_name}: Hi {first_name}, checking in after the message we sent to {provider}'s office through {portal_name}. If they "
        "answered and said to go ahead with the {test_category}, reply with a day that could work. If you're still waiting, reply WAIT."),
    "offer_slots": (
        "{partner_name}: {site_name} can book you on {when}. Open times: {slot_1} or {slot_2}. Reply 1 or 2 to book, or WALK IN to skip booking and just come by."),
    "offer_slot_one": (
        "{partner_name}: {site_name} can book you on {when}. The one open time that fits is {slot_1}. Reply 1 to book, or WALK IN to skip booking and just come by."),
    "booking_confirmed": (
        "{partner_name}: Booked: {when} at {slot_time}, {site_name}, {site_address}. Confirmation {confirmation_id}. We'll text a reminder the day "
        "before. Reply MOVE to change it. Reply STOP to opt out."),
    "booking_reminder": (
        "{partner_name}: Reminder: your booked visit is {when} at {slot_time}, {site_name}, {site_address} (confirmation {confirmation_id}). Reply MOVE to change it."),
    "booking_moved": (
        "{partner_name}: No problem, that booking is cancelled. What day or time would work better?"),
    "booking_move_failed": (
        "{partner_name}: I couldn't change that booking just now, so it still stands: {when} at {slot_time}, {site_name} (confirmation {confirmation_id}). "
        "A coordinator (a real person) will sort it out within one business day. If you can't make it, you can also call the location."),
    "booking_failed_walkin": (
        "{partner_name}: I couldn't book that time just now. You can still walk in at {site_name}, {site_address}, {when} during listed hours, "
        "or reply with another day and I'll try again."),
    # --- version 4: the resolver's replies (composable; every fact comes from the verified directory) -------------
    "offer_alternative": (
        "{partner_name}: {asked_line} The closest option is {alt_line}. Would that work?"),
    "offer_mobile_stop": (
        "{partner_name}: There's a {partner_name} mobile draw in {stop_town} on {stop_day}s, {stop_window}, at {stop_address}. "
        "No appointment needed. Would that work?"),
    "transport_resolved": (
        "{partner_name}: Understood. {transport_instruction} Want me to send the closest location's address and hours so you can book the ride?"),
    "out_of_area_ack": (
        "{partner_name}: That's outside the area where {partner_name} has a location I can send you to; the closest is "
        "{nearest_site}, about {nearest_miles} miles away. A coordinator (a real person) will text within one business day about options."),
    "location_link_offer": (
        "{partner_name}: Tell me the town or zip code where you'll be and I'll find the closest place. Or tap this link to "
        "share your location once (it's used only for this): {location_link}"),
    "unclear_menu": (
        "{partner_name}: Let me make this easy. Reply 1 for locations and hours, 2 if the lab work is already done, "
        "3 if you have a question for your care team, or STOP to end texts."),
    # --- fallbacks -------------------------------------------------------------------------------------
    "unclear": (
        "{partner_name}: Sorry, we didn't catch that. Reply with a day that works for lab work, a question, or STOP to opt out."),
    "handoff_generic": (
        "{partner_name}: Thanks. A coordinator (a real person) will follow up with you within one business day."),
}


def render(template_id: str, ctx: Dict) -> str:
    tpl = TEMPLATES[template_id]
    return tpl.format(**{k: (v if v is not None else "") for k, v in ctx.items()})


# --- offers, by construction (Sept 23; Codex V4) ---------------------------------------------------------------------
# An "offer" is any text that names a place, a time slot, a plan or a booking: it must not leave on a stale feed (the
# order may already be closed).  The set is DERIVED from the registry — any template whose placeholders name a site, a
# slot, a stop, a booking confirmation or an agreed time — plus the few that commit the patient to a plan without such a
# placeholder.  Hand-listing is what let `offer_slots` and `offer_slot_one` through.
_OFFER_PLACEHOLDER = re.compile(r"\{(site_\w*|slot_\w*|stop_\w*|mobile_\w*|confirmation_id|when|new_when|link_line)\}")
OFFER_TAGS: Set[str] = {"ask_day", "plan_confirmed", "reminder", "clarify_times"}


def offer_templates() -> Set[str]:
    return {k for k, v in TEMPLATES.items() if _OFFER_PLACEHOLDER.search(v)} | OFFER_TAGS
