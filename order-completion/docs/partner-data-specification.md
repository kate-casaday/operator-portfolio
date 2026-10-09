# Partner data specification — what StealthCo needs from a health system, and why

*Version 5 · September 16, 2026 · partner-facing draft · machine-readable schema: `data/schema/partner-data-v5.schema.json` · synthetic examples: `data/examples/`*

**Read this first.**  StealthCo helps patients complete the tests their clinician already ordered.  To do that well we need a
small amount of data to *start*, more to *answer particular questions or take particular actions*, and some that only
*improves* the experience.  This document lists each field, what depends on it, where it comes from, how fresh it must
be, and what the software does when it is missing, stale or contradictory.  Nothing here asserts what your vendor's
interfaces can do: every integration mechanism is a **partner decision**, and every field can start life as a **file
export** or, where marked, as **authorized manual chart review** by a StealthCo operator under your policies.

## Two authorizations, kept apart

| | Patient-authorized record connection | Partner-authorized operational integration |
|---|---|---|
| Who grants it | The patient (for their own record) | The health system (contract + security review) |
| What it permits | **Read** the patient's record for the purposes they authorized: orders, notes, care team, instructions | The order feed, order events, the verified lab directory, escalation routes, **messaging on your behalf**, and **booking**, each granted separately |
| What it does *not* permit | Sending messages to the patient as you, writing to your inbox, booking, or reading other patients | Anything the contract does not name.  Record access under this heading is *minimum necessary* for the operational purpose |
| In this prototype | Simulated as `synthetic_notes.json` plus a labeled manual chart-review workflow | Simulated feeds, a simulated portal adapter, a simulated scheduler.  None sends, writes or books anything real |

Access to records never implies permission to message or to book.  The software enforces this with separate capability
flags in the partner record (`capabilities.messaging`, `capabilities.booking`, `capabilities.record_read`), all of which
default to off.

## Tier 1 — minimum to initiate outreach

Without every field in this tier the software does not text the patient.

| Field | Purpose / dependent behavior | Source of truth · provenance | Required · missing values | Freshness · update | When missing / stale / conflicting | Manual review may supply |
|---|---|---|---|---|---|---|
| `partner_id`, `feed.generated_at` | Identifies the feed and the freshness clock; outreach pauses when the newest feed is older than the stale threshold (48 h default) | Partner export job | Required | Every export; daily minimum | Missing → feed rejected.  Stale → outreach and offers pause; safety replies still go | No |
| `patient.source_patient_id` | Stable identity for de-duplication and for every event that follows | Partner MPI / EHR | Required; never blank | Stable | Missing → record rejected | No |
| `patient.display_name` | Greeting; identity check at the phone | EHR demographics | Required; preferred name allowed | On change | Missing → record rejected | No |
| `patient.phone` | The only channel; a number shared by two patients quarantines both | EHR demographics | Required for outreach; `null` = no outreach, record kept | On change (a change cancels queued texts) | Missing → ineligible, listed on the operator page | No (a phone must come from the record of truth) |
| `patient.consent_sms` | Partner's own record that the patient may be texted about care | Partner consent registry | Required boolean | On change | `false`/missing → no outreach.  StealthCo's own STOP overrides `true` forever | No |
| `patient.home_town` (or postal address / zip) | Nearest-site ranking and the service-area rule; the map | EHR demographics | Required (town or zip); street address optional | On change | Missing → sites listed in directory order, out-of-area rule cannot run, flagged | No |
| `order.source_order_id`, `order.ordered_at`, `order.priority` | Identity, the 45-day threshold, exclusion of non-routine orders | Order system | Required | On placement | Missing → order rejected.  Non-routine → excluded and listed | No |
| `order.lines[].test_code`, `test_name` | Which collection services a site must offer; what the patient may be told (category only for sensitive tests) | Order system + the partner-approved service catalog | Required; a code the catalog does not map **fails closed** (no site offered; mapping item for a person) | On placement | Unknown code → mapping review; conflicting names → the code wins, the name is flagged | Mapping (not the code) may be confirmed by a person |
| `order.ordering_clinician` (name + role) | Whose office the first text speaks for; default receiving team for clinical questions | Order system | Required | On placement | Missing → "your care team"; clinical routing becomes *ambiguous* and is flagged on the referral dashboard | No |
| `order.state` at export (open / cancelled / resulted) with `intended_due_at` if any | Eligibility; an order awaiting a future intended date is never overdue | Order system | Required; `intended_due_at` optional | Every export | Conflict between feed state and later events → the newer authorized event wins; both are kept in `order_events` | No |
| `directory.sites[]` (id, name, address, town, hours, services, `verified_at`, `verified_by`) | The only places a patient may be sent; hours and capabilities checked at the moment of use | Partner lab operations, **signed** | Required; unsigned or > 90 days old → never offered | Re-verify every 90 days or on change | Stale → site withheld; if none remain, a "no verified site" item for a person | Yes — a person may re-verify by phone and sign the record |
| `directory.clinician_contact.phone` | The urgent line in every safety text | Partner | Required | On change | Missing → outreach does not start | No |
| `capabilities.messaging` | Whether StealthCo may text as the partner (sender identity, consent wording) | Contract | Required boolean | Contract change | `false` → nothing is sent; the prototype simulates | No |

## Tier 2 — needed to answer particular questions or take particular actions

Missing data here never blocks outreach; it changes what the software can say or do, and the gap is visible.

| Field | Enables | Source · provenance | Required · missing values | Freshness | When missing / stale / conflicting | Manual review may supply |
|---|---|---|---|---|---|---|
| `order.rationale` — the documented reason, as written (`source_ref`, `excerpt`, `author`, `authored_at`) | Answering "why was this ordered?" with the note's own words, attributed | Clinician note (record connection) **or** authorized chart review producing a **fact card** | Optional.  Missing → the reply says the reason is not written in what we can see and points to the clinician's office; it never guesses | On new notes | Two notes disagree → an *unresolved* card, flagged for partner clinical clarification; nothing is told to the patient | **Yes** (proposal only; approval requires the designated clinical reviewer) |
| `prep_instructions[test_code]` (text, `approved_by`, `approved_at`) | Answering "do I need to fast?" from your approved text, verbatim | Partner clinical operations reference | Optional per test.  Missing for any open line → the question goes to the clinical route | Re-approve yearly or on change | Stale (> 365 days) → treated as missing | Copying partner reference text: operator.  Patient-specific prep from a note: clinical reviewer |
| `care_team` per order (`ordering_clinician`, `pcp`, `episode_team`, each with a contact route) | Routing a referral to the intended team; the dashboard's "receiving team" | EHR care-team record | Optional beyond the ordering clinician | On change | Missing or conflicting → routing *ambiguous*, flagged, default to the ordering clinician's office | Operator may record what the partner tells them (administrative) |
| `escalation_routes` (reason → team, response window, channel) | Which team a clinical question, staff request or emergency notification goes to, and by when | Partner policy, **signed** | Optional; default = ordering clinician's portal, 1 business day | On change | Missing → defaults with `pending_partner_approval` shown on the dashboard | No (policy) |
| `patient_portal` (name, link, `approved_at`) | The portal-link handoff; relay mode | Partner | Optional.  Missing → clinic phone only; no promised clinical response | On change | Stale → treated as missing | No |
| `capabilities.portal_relay` | Whether StealthCo may send a patient-authored message into your portal | Contract + vendor capability **(partner decides; not assumed)** | Optional boolean, default `false` | Contract change | `false` → relay never offered | No |
| `capabilities.clinician_queue` (+ owner, SLA) | Whether the partner accepts StealthCo's clinician queue | Contract | Optional boolean, default `false` | Contract change | `false` and no portal → clinic phone only | No |
| `scheduling` per site (`adapter`, slot length, lead time) | Booking instead of a walk-in plan | Partner scheduling system **(partner decides; simulated here)** | Optional.  Missing → walk-in plans only, never called appointments | On change | Failed booking → walk-in plan offered, booking failure recorded | No |
| `order_events` stream (`result_finalized`, `cancelled`, `replaced`, `modified`, `attended`) | Verified completion; closing the *right* orders; reconciling a patient-reported change; attendance | Order + lab systems | Optional beyond result/cancel; `replaced` needs the new order in the same feed | Daily minimum | Duplicate event id → ignored; event for an unknown order → held and replayed; `attended` never counts as completion | No |
| `urgent_care[]` (verified) | Named in the emergency text as **non-emergency urgent care**; 911 is the emergency guidance regardless | Partner, signed | Optional | Re-verify 90 days | Missing → the clinic phone | Person may re-verify |
| `transport_program` (approved text) | Solving a transport barrier without a person | Partner, signed | Optional | Re-approve 90 days | Missing → a person | No |
| `mobile_routes[]` (stops, days, windows, services, signed) | Offering a mobile draw in the patient's town | Partner / StealthCo operations | Optional | Weekly | Stale → not offered | Person may verify |

## Tier 3 — enrichment

| Field | Improves | Source | Notes |
|---|---|---|---|
| `patient.language`, `patient.contact_preferences` (best hours, caregiver contact with authorization) | Tone, timing, who is greeted | EHR / patient statement | Caregiver authorization is a partner record, not a patient text |
| `patient.prior_visits` at partner sites | Which site to name first | Lab system | Never told to the patient as a fact unless from the record |
| `order.visit_at` | "Regarding your visit on Jul 20" in the first text | Encounter record | Missing → omitted, never invented |
| `general_education[test_code]` (partner-approved plain-language description) | "What is a CBC?" answered from your text | Partner patient-education library, approved | Class **educational**, distinct from the patient's documented rationale |
| `completion_evidence` detail (site, collector, specimen id) | Auditing which visit produced which result | Lab system | Never shown to the patient |

## Behavior rules that follow from the specification

- **Missing beats invented.**  Any field the software would have to guess is treated as absent and the patient is told what we cannot see.
- **Newest authorized event wins; nothing is overwritten.**  Every order change is appended to `order_events`; the patient's own report of a change pauses outreach and opens reconciliation but never changes the target.
- **Signed means dated and named.**  Sites, instructions, routes and prep text carry `verified_by` / `approved_by` and a date; the software refuses unsigned or expired records at the moment of use.
- **Chart review is quotation.**  A reviewer copies the note's sentence and its reference; the card records who, when and by what method.  Approval authority follows the card's class.
- **Record read ≠ message ≠ book.**  Three capability flags, three contracts.

## Open partner decisions (recorded, not assumed)

1. Which interfaces exist for the feed, order events, portal messaging and scheduling, and what each permits.
2. Who the designated clinical reviewer is for rationale and prep cards.
3. The urgency and escalation criteria (the prototype ships a draft marked *pending clinical approval*).
4. Which team owns each referral kind and the response window.
5. Whether notes arrive through a patient-authorized connection, a partner extract, or authorized manual review.
6. Sender identity and consent wording for texts.
