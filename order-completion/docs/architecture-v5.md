# Chief of Health — architecture note, version 5 (Sept 16, 2026)

*Synthetic prototype.  Status words used throughout: **functional** (implemented and tested here), **simulated** (a real interface with a stand-in behind it, labeled on screen), **partner-dependent** (needs a decision or an integration from a health system), **unverified** (a claim carried from an earlier document that this build did not check).*

## 1. The shape

```
partner data (spec: docs/partner-data-specification.md; schema: data/schema/partner-data-v5.schema.json)
   orders feed · order events · care teams · clinical notes (record connection OR authorized manual review) · directory · routes · policies
        │ feed_integrity.py (Sept 24: pull/push arrival → 16 checks → quarantine / hold → receipts · health · alerts → partner technical contact)
        │ importer.py (idempotent; append-only order_events)      │ facts.py (notes → proposed cards → reviewed cards)
        ▼                                                          ▼
   SQLite: patients · orders(+superseded_by, care_team) · order_lines · order_events · conversations · messages · events ·
           escalations · fact_cards · fact_reviews · clinical_notes · referrals · referral_events · bookings · portal_messages ·
           patient_locations · feedback · eval_cases · candidate_changes · release_history · model_calls · human_time ·
           feed_receipts · feed_health · feed_alerts (Sept 24)
        ▲
        │ engine.py — the orchestrator; facts.py / referrals.py / scheduling.py / improve.py write their own tables through the same
        │            connection and event log (Codex V5-10: "the only writer" was inaccurate).  rules.py — what is allowed.
        │   inbound: STOP/HELP/wrong-number/emergency/menu-digit/slot-digit/WALK IN/MOVE decided in code → held-state rules →
        │            usage/spend ceilings → classify (mock | Claude) → validate → dispatch → (resolver → reviewer) → template or composed text
        │   tick:    cadence, reminders (walk-in or booking), verify deadlines, clinical follow-ups by pause reason, referral lifecycle,
        │            portal-crash reconciliation, outbox flush
        │
   adapters (all interchangeable): llm/ (mock | anthropic | routed; composer; resolver + reviewer: rules | claude | openai)
                                   messaging/ (simulated | twilio, send disabled)   portal.py (simulated)   scheduling.py (simulated)   geo.py (local centroids)
        │
   surfaces: /  Operations   /conversation/<id>   /referrals(/<id>)   /facts(/<id>)   /improve   /where/<token>
```

## 2. What is functional, simulated, partner-dependent (v5)

| Area | Functional | Simulated | Partner-dependent |
|---|---|---|---|
| Partner data | Schema, three tiers, examples validated, importer for every event kind, capability flags enforced in the engine | Feeds are JSON files | Every interface; consent language; retention |
| Feed integrity (Sept 24, reconciled) | Arrival paths (inbox pickup, push endpoint, updates API) all validated, both streams of a payload, before one atomic import (`Engine.import_validated`; module schemas created at startup, never inside a transaction); row and event-payload checks, quarantine with per-patient holds and the receipt itself written inside the import transaction (a receipt says rejected only when nothing was committed), held files, replay (a no-op on state) / conflict / late-arrival / stalled distinguished, per-partner health with a moving baseline trained only on unremarkable files, one send gate (`Engine._feed_gate`) in `_flush_outbox` that withholds every non-compliance, non-safety message except `hold_ack` under a block or a hold (`hold_ack` also passes the automatic integrity pause, never a person's), the stale-feed offer set derived from the template registry (`templates.offer_templates()`), composed with the stale pause, late-file (any delay, never-started streams) and silent-results detection closed on recovery, one alert per incident with the exact partner message (identifiers only), named acknowledge/resolve/override, retained payloads with an events-only reprocess, Operations section, `GET /api/feed`; 37 tests (`tests/test_feed_integrity.py`) including atomicity, mixed streams, queue→reject→flush, slot offers without a global pause, replay no-op, stale→integrity, operator resume while blocked | The notifier and the pickup folder (runs when the tick runs; nothing schedules itself; at-least-once with no-op replay); the partner technical contact on the directory | Transport and credentials; contact and channel; thresholds from real files; the unknown-kind blocking policy and FYI cadence (Kate).  Design record: an internal feed-integrity brief (not included) |
| Fact cards | Versioned cards with provenance, verbatim-excerpt rule, conflict groups, role-gated approval (operator vs clinical reviewer), supersession chain, chart-review minutes, review UI | The role switch; the rule extractor (a live extractor is an interface only) | Who the designated clinical reviewer is; whether notes arrive by record connection or manual review |
| Longitudinal orders | `order_events` for imported / result / cancelled / replaced / modified / attended / patient_reported_change; replacement inherits eligibility; patient-reported change pauses + reconciles without changing the target; reason-specific pause follow-ups | — | Event kinds a partner can actually emit |
| Referrals | One record per handoff kind with lifecycle, delivery evidence, routing basis, ambiguity flag, urgency under a pending policy, duplicates, overdue, next-day emergency task, resolution gated by basis + authority, filters, audit | Partner evidence is entered by hand; relay delivery is `simulated` | Routes, urgency, criteria approval; portal write capability |
| Communication | Rationale answered only from approved documented cards or an honest gap; care-team roles distinct; emergency = immediate 911 text, partner item, separate next-day task; urgent care named as non-emergency care; disclosure of what was actually sent | — | Whether relay is permitted at all |
| Booking | Slots offered at bookable sites, book / walk-in / MOVE / failure paths, four outcomes tracked separately | The scheduler (deterministic availability) | A scheduling integration and its permissions |
| Improvement | Flag message / card / referral with a category; versioned evaluation cases with machine checks; candidate changes with evaluation runs, approval, release tag, rollback, history | — | Release process once a second engineer exists |
| Measures | Experience proxies, completion funnel, clinician-facing items per 100, labeled missed/unnecessary escalation, staff minutes, chart-review minutes | — | Real replies and a survey |

## 3. Assertions audit — claims inherited from earlier briefs

| Claim (where it appeared) | Status in this build | What would verify it |
|---|---|---|
| Epic exposes patient-portal message creation to third parties as a FHIR-style vendor service (v4 brief §1) | **Unverified.**  Not checked against vendor documentation; the relay adapter is an interface with a simulation behind it | Read Epic's open API catalogue for the specific patient-message API and its App Market terms; confirm with the partner's Epic team |
| Oracle Health / athenahealth have equivalent write APIs (v4 brief §1) | **Unverified** | Same, per vendor |
| The Census geocoder is free, keyless and suitable (v4 brief §3, `geo.py`) | **Unverified.**  Never called; the class is a placeholder | Call it in a sandbox with a synthetic address; read its terms |
| Opus 5 is "the strongest model available under a zero-data-retention BAA"; Fable is ineligible (platform-futureproofing §1) | **Unverified this session.**  Console/BAA terms were not re-read | Re-read the Console's BAA configuration page and the model eligibility list |
| Model prices and prompt-cache minimums in `costs/prices.json` | Read once on Sept 15 from the pricing page; **not re-checked** | Re-read before any cost claim leaves the building |
| Health systems accept portal-link routing as governance-friendly (v4 brief §1) | **Belief, untested** with any partner | One CMIO conversation |
| Twilio failure classes / signature validation (README) | Unit-tested against a patched HTTP layer; **signature check not verified against a known-good vector** | Run Twilio's validator sample |
| Anthropic SDK behaviours (structured output `output_config`, `effort`) exercised live on Sept 15 | Verified then; **not re-run in v5** (no paid calls) | One paid wording eval |
| "Kate and an algorithm" workload numbers in `costs/` | Assumptions, labeled | A pilot month |
| §2's "functional" rows as first written on Sept 16 | **Amended after Codex's closing review (V5-1..V5-9):** capability flags were stored but not enforced; documented cards accepted a missing source; the importer ignored feed order state; reconciliation resumed on attendance; idiom exclusions swallowed real signals; slots ignored the patient's time bounds; a failed cancellation read as success; evidence was a label; the improvement ledger could certify unchecked expectations; the example payloads did not import.  All ten reconciled the same day with regressions (`tests/test_v5.py::V5_Reconciliation`); the rows above describe the reconciled state | Codex's focused verification (Kate authorizes) |

## 4. Security and traceability posture (prototype)

Local only (127.0.0.1, no auth, SQLite); the operator / clinical-reviewer role is a simulated switch.  This is a synthetic
prototype, **not a demonstrated secure clinical platform**.  No PHI anywhere.  Every decision writes an event; every referral, card and
change carries an audit trail; feedback evidence is captured at flag time and survives resets.  Adapters are interchangeable.  The
model never decides scheduling facts, completion or consent; it *classifies* intent, and that classification does route clinical
questions — the code-only screens (STOP, emergency wording, clinical keywords when the model is unavailable) are the floor beneath it,
not a claim that routing is model-free (Codex V5-10).  For the pilot path (BAAs, secrets, hosting, CI) see an internal platform note (not included).
