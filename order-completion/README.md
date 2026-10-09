# Order-completion prototype (version 5, synthetic only)

A text-based application that helps patients complete outstanding lab orders with minimal operational
intervention and explicit clinical escalation, plus a work surface for Kate to inspect and improve the patient
experience without writing code.  Design constraint: **"Kate and an algorithm."**

Everything here is synthetic.  No real patient, partner, phone number, site or price appears in `data/`.

## Start here

Python 3.11.  Mock mode uses only the standard library and needs no credentials.  From the repository root:

```bash
cd order-completion
python3.11 -m unittest discover -s tests -t .     # full test suite, mock mode (257 tests)
python3.11 -m ocp demo --quiet                     # 41 scripted synthetic scenarios → PASS/FAIL ledger
python3.11 -m ocp seed                             # fresh synthetic session: 25 interactive patients, first outreach sent
python3.11 -m ocp serve                            # Operations page + Conversation pages on http://127.0.0.1:8765, mock mode, no cost
```

Open http://127.0.0.1:8765 and choose an active conversation.  The demo runs the scripted scenarios and replaces the selected database, including feedback stored there.  Seed then resets the conversations for an interactive session and preserves feedback rows.  Run these commands before collecting feedback you want to keep in its original conversation.

Live modes, the paid evaluations, and the cost scripts (`costs/`) are described at the end of this file.  The live-mode wrapper `bin/run.sh` expects a project virtual environment with the Anthropic SDK installed.

### What changed on Sept 23, 2026 — feed integrity; brief: internal, not included

- **Operating principle (Kate):** never outsource to the health system's technology team work we can do ourselves.  `ocp/feed_integrity.py`,
  reconciled the same day after Codex's first review: every partner payload arrives through a validated path (drop a file in `var/feed_inbox/`
  and it is picked up **when the scheduler tick runs** — nothing schedules the tick in this prototype; `POST /api/feed/receive`;
  `POST /api/import/updates`), both streams validated before **one atomic import** (a held file applies nothing; the receipt and the per-patient holds live in the same
  transaction, so a receipt says rejected only when nothing was committed), bad rows quarantined with the patients they touch held from every
  non-safety message, one send gate that withholds every message except compliance, safety and the hold acknowledgement under a block or a
  hold (the acknowledgement also passes the automatic pause, never a person's), the stale-feed offer set derived from the template registry, a replay of an accepted file treated as a no-op on state, a late file and a
  silent results stream noticed by us and closed by us on recovery, the original payload retained and its events stream reprocessable by a
  named operator, and one message per incident to the partner's technical contact (simulated notifier; identifiers only; text stored on the
  alert).  Pickup is at-least-once with no-op replay, not exactly-once.  Operations page section **Feed integrity**; `GET /api/feed`.
  `tests/test_feed_integrity.py` (37 tests; suite now 257).

### What changed in version 5 (Sept 16, 2026) — brief: internal, not included; architecture + assertions audit: `docs/architecture-v5.md`

- **Partner data specification** (`docs/partner-data-specification.md`, `data/schema/partner-data-v5.schema.json`, `data/examples/`):
  three tiers (minimum to initiate; needed for particular answers/actions; enrichment), each field with purpose, source,
  requiredness, freshness, behaviour when missing/stale/conflicting, and whether authorized manual chart review may supply
  it.  A **patient-authorized record connection** and a **partner-authorized operational integration** are separate
  capability flags; record read never implies messaging or booking.  No vendor API capability is asserted.
- **Fact cards and chart review** (`ocp/facts.py`; pages `/facts`, `/facts/<id>`): versioned cards with source reference,
  verbatim excerpt, author/date, extraction method, review status, reviewer and role, verification time, supersession chain
  and conflict groups.  Classes **documented / educational / unresolved**.  Rule extraction is quotation: a rationale card's
  statement is the note's own sentence, attributed; a paraphrase is refused.  **Roles:** the operator proposes, edits, flags
  and approves administrative cards; **rationale cards require the designated clinical reviewer** (a labeled simulated
  switch here).  Unresolved cards cannot be approved by anyone.  Chart-review minutes are logged per review.
- **"Why was this ordered?"** is answered only from an approved documented card (`rationale_documented`: "Dr. Okafor's note
  from Jul 19 says: '…'") or with an honest gap (`rationale_unknown`) plus a referral that shows `missing: order_rationale`
  on the dashboard.  Never "your doctor ordered this because…".  Consequential details are app-rendered; the model still
  writes the conversational parts; the lexical checker remains a guard, not a proof.
- **Longitudinal order state** (`order_events`, append-only): `imported`, `result_finalized`, `cancelled`, `replaced` (the
  replacement inherits the replaced order's standing and the conversation continues on it), `modified`, `attended`
  (evidence, never completion), `patient_reported_change`.  A patient saying the plan changed **pauses outreach and opens
  reconciliation without changing the target**; only a provider event resumes (approved wording states what is on file
  now) or closes.  Follow-ups after a pause are chosen by the reason: portal referral → quiet days then one follow-up; relay
  sent → "if they answered and said go ahead…"; plan change → wait for the provider; emergency → a person decides.
- **Clinical referral dashboard** (`ocp/referrals.py`; `/referrals`, `/referrals/<id>`): one record per handoff —
  portal-link-only, relay offer, relay sent, clinician queue, emergency, route missing, plan change — with lifecycle
  (`offered`, `awaiting_patient`, `sent`, `queued`, `acknowledged`, `responded`, `resolved`, `failed`, `expired`,
  `cancelled`), delivery evidence (**portal-link handoffs stay `unverified`**), the patient's own words, a labeled rule-built
  summary, intended receiving team and routing basis (ordering clinician / PCP / episode team / nurse line; ambiguity
  flagged), urgency under a policy marked **pending clinical approval**, owner, next action, response window, overdue and
  duplicate flags, partner evidence entered by hand, and an audit trail.  **Resolution needs a basis and authority:** the
  operator cannot resolve a clinical referral without recorded partner response evidence or the clinical reviewer.
- **Emergency:** the 911 text goes out first and never waits; the partner item and a **separate next-day follow-up task**
  follow; urgent care is named as care for a non-emergency urgent problem.  Escalation criteria are shown as
  *pending clinical approval*.
- **Booking** (`ocp/scheduling.py`, simulated): at a bookable site a confirmed day yields two open times (`offer_slots`);
  `1`/`2` books (confirmation id; `booking_confirmed`), WALK IN keeps a walk-in plan, MOVE cancels, failures fall back to
  the walk-in plan.  The funnel counts **intention, confirmed booking, attendance, verified completion** separately.
- **Improvement** (`ocp/improve.py`; `/improve`): flag a message, a fact card or a referral with a category (tone,
  factual support, operational effectiveness, routing, longitudinal order handling); reviewed feedback becomes a versioned
  evaluation case with machine checks (a free-text expectation never passes by itself); candidate changes carry the
  feedback they address, evaluation results on a fresh engine, approval, release tag, rollback and history.  Interaction
  logs, evaluation cases and training data (none exists) stay separate; nothing deploys from feedback automatically.
- **Measures on Operations:** experience proxies, completion funnel, clinician-facing items per 100 conversations, labeled
  missed / unnecessary escalation, staff minutes, chart-review minutes.  Not optimized solely for fewer referrals.
- **After Codex's closing review of version 5 (same evening), all ten findings reconciled with regressions
  (`tests/test_v5.py::V5_Reconciliation`):** partner capability flags are enforced, default deny, at the send boundary,
  for booking, relay, the clinician queue and notes import (V5-1); a documented card must quote an existing note that belongs
  to the patient, attribution comes from the note, clinical content in any kind needs the reviewer, sources are re-verified
  at approval and at use, and approving one side of a conflict requires the reviewer and a resolution note that rejects the
  other side (V5-2); the importer honours the feed's order state and line status, applies structural events in chronology
  order, and derives event ids from the full payload (V5-3); reconciliation resumes only on a cancellation, replacement,
  modification or result dated after the patient's report, and a replacement is re-screened for everything except its age
  (V5-4); emergency idioms are removed from the text before the screen runs, and a model "emergency" on a benign idiom is
  downgraded (V5-5); slots honour the patient's time bounds and the timed-test limit, are revalidated at booking, and any
  other reply supersedes the offer (V5-6); a refused cancellation keeps the booking, says so, and opens an item (V5-7);
  partner evidence needs a source reference and a summary, terminal referral states hold, and `resolved` is reachable only
  through `resolve()` (V5-8); expectation keys are validated, a pending free-text check keeps a case at `needs_review`,
  `booking_state` is implemented, approval needs passing reviewed cases or an explicit recorded exception, and a replay
  restores approved cards and provider events first (V5-9); the v5 envelope (`patients` + `orders`, `order_events`) imports
  end to end and unsupported envelopes are rejected (V5-10).
- **Test policy:** `walkin_policy()` pins the pre-booking journey for tests about walk-in plan mechanics (3 tests);
  `tests/test_v5.py` (15 tests) exercises the v5 defaults; scenario 15 now shows the walk-in choice; `cw-04`/`cw-08`
  expect `offer_slots`.  Cohort: 29 patients / 25 interactive (P-24, P-25 added for v5 scenarios).

### What changed in version 4 (Sept 16, 2026) — "Claude solves the problem"; brief: internal, not included

- **Clinical handoff through the partner's portal** (`Policy.clinical_handoff`, Operations setting).  Default
  **`portal_link`**: a clinical question or a request for clinical staff gets approved wording with the partner's
  portal link and phone (911 line included); **no queue item**; the conversation is not held; outreach goes quiet for
  `clinical_pause_days` (7) and then sends one gentle follow-up.  **`portal_relay`**: the assistant offers to send the
  question to the provider's office through the portal *as a message from the patient*; the next text is captured
  **verbatim** and sent through a portal adapter (simulated here, like Twilio; production adapters are partner-specific);
  the model never writes any of it.  **`clinician_queue`** is version 3.  Without an approved portal link in the
  directory every mode falls back to the clinician queue.  Model-unavailable exits and held conversations follow the
  same mode.
- **Preparation questions are answered by the application** from partner-approved prep text in the service catalog
  (verbatim, only when every open line has approved text), with the portal link for anything further.  A medication
  question that mentions timing is not a preparation question.
- **Emergency wording is decided by code before any model**, any hold, any pause: an approved safety text with 911 and
  the nearest partner-verified urgent care (by distance from the patient's location) goes out; the conversation is
  held; the partner gets an emergency-priority item (`emergency_notify_partner`).
- **The operational resolver** (`ocp/llm/resolver.py`; `Policy.resolver_mode` off | shadow | on, default on).  Before a
  Kate item opens for a *solvable* dead end (a schedule no site fits, a transport barrier, an out-of-area patient) the
  application builds a **menu of verified options** (a site that fits if one stated constraint is relaxed, a mobile
  draw stop in or near the patient's town, the approved ride program, the nearest site when just outside the radius);
  the resolver picks one and says why; an **independent reviewer** (rules by default; a second Claude model or an
  OpenAI model when live) agrees or disagrees on judgment only; agreement executes the option through the ordinary
  offer code, disagreement or "escalate" opens the Kate item with what was tried attached.  Cost, "already done",
  unmapped codes, identity and abuse are never resolvable.
- **Two unclear replies get a numbered menu** before any person; the digit is decided by code; a third unclear reply
  goes to a person.
- **Geo** (`ocp/geo.py`).  A zip or a town the patient names is geocoded (local synthetic centroids; the interface is
  where a production geocoder goes) and stored with its source and an expiry; sites are ranked from that point with
  **distances on the fact sheet**; a place with no capable site within `out_of_area_miles` (25) goes to the resolver,
  then to Kate with the nearest site and distance; a patient who says they are not near home and names no place gets
  the **consent-based one-time location-share page** (`/where/<token>`, single use, expiring, bound to one
  conversation).  Carrier location does not exist for A2P SMS senders; IP geolocation is not used.
- **Shadow planner.**  The classifier also returns `proposed_action` (from a closed set); the engine records it next to
  the action the rules chose (`planner_shadow` events; agreement on Operations).  Nothing acts on it.
- **Operations page: Automation section** (clinician-facing items per 100 conversations, Kate items per 100, portal
  referrals, prep answers, relays, emergency texts, resolver tried/solved/disagreements, menu digits, locations,
  planner agreement) with the handoff mode, resolver mode and quiet-days controls.  The map draws mobile-route stops.
- **v3 residuals from Codex's Sept 16 focused verification:** one timing rule in the fact sheet (site limit *and*
  catalog default, the earlier wins, as in selection); realized opener variant derived from the **sent text**; honest
  interior times ("Monday at 10am" inside 7am–4pm) are accepted by the fact check; a vocabulary patch for the fresh
  bypasses Codex found (sensitive aliases, medication names, price synonyms) — the boundary remains lexical, and
  application-rendered fact cards remain the structural answer for Kate to decide.
- **After Codex's closing review of version 4 (same day):** a constraint the patient calls absolute ("the only day", "must",
  "nothing else") is recorded as hard and is never relaxed by the resolver — refused in the menu, by the rules reviewer and
  again at execution (V4-1); a relay acknowledgement ("yes please") is consent, not the message — the assistant asks for the
  message itself, sends a substantive text exactly once, keeps the original question with it, and a crash after the portal
  accepted leaves an `ambiguous` row that is never resent (V4-2); the clinician queue is an explicit partner opt-in
  (`clinician_contact.accepts_queue`) — with no portal link and no opt-in the patient gets the clinic phone with no promised
  clinical response and Kate gets one configuration item (V4-3); every location source, including the shared device
  location, goes through one service-area rule (V4-4); the emergency phrase set is wider ("can't catch my breath", "want to
  die", "took too many pills", "face is drooping") with figurative uses excluded ("this bill is giving me chest pain") — a
  clinically governed set and recall measurement remain a partner item (V4-5); planner agreement counts only the exact
  action or a narrow same-work family, disposition (person or not) is recorded separately, and the rules reviewer is
  labeled "not a model" (V4-6).  Walk-in intentions are still not booked appointments (V4-7): a launch-claim decision.
- **Test policy:** three defaults changed (portal handoff, resolver on, unclear menu).  Version-3 tests that test the clinician-queue
  path or the escalation mechanics (23 engine constructions across six files) now pin `v3_policy()` (see `tests/helpers.py`) so they keep
  testing what they were written to test; `tests/test_v4.py` (33 tests) exercises the version 4 defaults.  Scenario 3
  (transport) and 19 (staff request) changed expectation to the new defaults; scenario 14 pins the clinician queue.

### What changed in version 3 (Sept 15, 2026)

- **Claude writes the words.**  The application still decides everything (eligibility, which sites, plan feasibility,
  holds, escalation, consent); the model receives a **fact sheet** and writes the message; **`fact_check`** is an
  allowlist: every weekday, time, number (digits or words), proper noun, address, lab or brand word, provider mention and
  clinical/results/price word must trace to the fact sheet or the patient's own words, days and times must fit the named
  site's hours, and each action's commitments (anchors) must be present.  It is a strong lexical boundary, not a proof.  A
  refused text is retried once with the violations, then the approved template is sent and the refusal recorded.
  Compliance confirmations and clinical/staff safety acknowledgements are never composed.
- **The first text is fact-fed and written to Kate's structure** (greet; who and why with the visit date when the
  feed has it; the capable sites near the patient's town with their hours; one ask; STOP).  Every conversation records
  its **opener variant** (disclosure wording none/short, sites named, visit date present) and the Operations page
  reports reply, plan and completion rates by variant.
- **Lab capability matching.**  Sites carry `services`; orders map to `service_requirements` through
  `data/service_catalog.json` (blood draw, urine, DOT drug screen, timed glucose tolerance with a latest start,
  pediatric).  A site is offered only if it can perform the order; sensitive tests are never named.
- **Two pages.**  `/` is Operations (active conversations, escalations by path and owner, the partner data
  connection with late = red, patients and orders by state, a map of patients and labs with a slot for a mobile
  route, first-text variants, who wrote the words, spend, controls).  `/conversation/<id>` is one phone-like thread
  with Flag under each reply and "why" behind a click.  The round-two dashboard is at `/legacy`.
- **Spend brake in the application** (`Policy.max_spend_usd_per_day`, default $5 over a rolling 24 h): live
  composition stops and replies route to a person when it is reached.  Simulated writers cost nothing and are not
  counted as model calls.

### Five-minute walkthrough: play a patient, flag a response, save feedback, export a change request

1. `bin/run.sh`, open http://127.0.0.1:8765.  (Click *Fresh synthetic session* at any time: conversations reset,
   feedback rows are kept and archived.)
2. On **Operations**, click any active conversation; after a fresh synthetic session the initial outreach has already been sent, written from the fact sheet.
3. On the **Conversation** page type as the patient, e.g. `I work until 6 and only have Thursdays`, then
   `Actually, Saturday morning would be easier`.  The reply comes from the same code path a real text would take.
   Under each automated message, *why* shows the decision that produced it (rule, intent, effective constraints,
   sites considered, who wrote the words, any fact-check refusal).
4. Click **Flag** on the reply, choose *defect / wording / good example / policy question*, write what should
   have happened in plain English, *Save flag*.  The application captures the evidence at that moment:
   the thread through the flagged message, the patient's known constraints (with their source), order and
   workflow state, the next action and its reason, the decision record, the template, the adapter/policy
   configuration and the software version.
5. In **Feedback**, click *export task*.  A Markdown task lands in `feedback/exports/task-NNN-*.md` with
   *Kate's interpretation* and *Evidence (captured automatically)* in separate sections, drafted acceptance
   criteria and a regression-test stub.  Hand it to Claude Code or Codex.  Use *status* to mark it `linked`
   (commit / test) and later `verified`, so the change stays tied to the observation that caused it.

Export is refused unless every patient in the evidence is marked synthetic by the feed (`"synthetic": true`).

## What is implemented, simulated, prepared, partner-dependent, or still a hypothesis

| Status | What |
|---|---|
| **Implemented and tested** (`tests/`; `python3.11 -m ocp demo`, 41/41) | **Version 5:** partner data schema and examples, fact cards with role-gated chart review, append-only order history with replacement / modification / attendance and patient-reported change reconciliation, referral records with lifecycle and gated resolution, simulated booking with four separate outcomes, feedback → versioned evaluation cases → candidate changes with release history, the v5 measures.  **Version 4:** clinical handoff modes (portal link / portal relay with a simulated portal adapter / clinician queue), clinical pause and follow-up, approved prep answers, code-decided emergency text with nearest urgent care, the operational resolver with menu-building, rules resolver and rules reviewer, live resolver and reviewer adapters (Claude; OpenAI prepared, not exercised), the unclear menu, geocoding with stored patient locations, distances on the fact sheet, out-of-area handling, the location-share page, the shadow planner, the Automation section.  **Version 3:** composer contract (fact sheet → model text → fact check → template fallback), deterministic fact writer, lab capability matching with timed-test start limits, fact-fed opener with variant assignment and variant analytics, per-action commitments (`ACTION_GUIDE`), application spend brake, Operations and Conversation pages.  **Rounds 1–2:** order import with a configurable 15–45-day overdue threshold and intended due dates; eligibility and consent rules; outreach cadence with quiet hours, pause, per-partner stale-feed holds; intent handling for locations, hours, scheduling constraints (combined, corrected, evenings-or-weekends), transport, cost, clinical questions, requests for clinical staff or a real person, fewer reminders, completion claims (in/out of network), STOP/HELP/wrong number; preferences with source, statement link and correction history; verified-directory-only offers with content-approval revalidation at send; plans that are concrete dates checked against site hours and stated time constraints; escalation queues (Kate's operational queue, partner clinician queue) with assignee, business-hour deadlines, overdue flags, accepted status; commit-before-call send protocol with ambiguous-outcome handling; inbound recovery bound to the original sender; per-conversation ceilings; decision records on every outbound; the feedback loop and task export; the HTTP dashboard and API |
| **Simulated** (labeled as such on screen) | The scheduler (version 5; deterministic availability, in-memory bookings); the operator/clinical-reviewer role switch; partner evidence on referrals (entered by hand); the rule extractor's stand-in for a live extractor; the portal adapter (version 4: the relayed message is stored and "sent" in memory; no health system receives it); the geocoder (a local centroid table); the model in mock mode (a deterministic rule-based classifier and a deterministic fact writer stand in; simulated attempts are labeled and cost nothing); SMS transport (nothing leaves the process); the clinician handoff (a queue row with a due time — **nobody is notified, and clicking "accept" notifies nobody either**; it records that a person took the item); the partner feed (JSON files); the clock |
| **Exercised live on Sept 15, 2026 (synthetic patients, Kate's demo workspace)** | The live classifier (`ocp/llm/anthropic_adapter.py`): held-out intent agreement **17/19 on Claude Opus 5** ($0.05; the two misses are `willing` vs `confirm_plan` on "yeah I could do the one in bath, thursday after work maybe 5" and "Friday morning works, the Bath one").  The live writer (`ocp/llm/composer.py`): wording evaluation **10/10 on Claude Opus 5** and **10/10 on Claude Sonnet 5**, and **20/20 openers** with zero fact-check fallbacks on Opus 5, re-run after the Codex closing review against the **allowlist** fact check (every day, time, number, proper noun, address, lab word, provider mention and clinical/price word must trace to the fact sheet or the patient's words; per-action commitments are anchors).  Total spent building, reviewing and evaluating version 3: about $2.75.  The run logs are not included in this portfolio.md` §3 (vendor portal APIs, the Census geocoder, BAA/model eligibility, prices); that chart review by an operator is acceptable to a partner's privacy office; that "documented reason, quoted" satisfies patients; that a health system will accept the portal-link handoff as "not jamming the portal" (it is the patient writing, as today) and later the relay; that the resolver's menus cover most real dead ends; that the planner's shadow agreement is high enough to promote; that the live results hold on real patient language (the held-out set is 19 synthetic replies written by the people who wrote the prompt; the wording set is 10 synthetic threads); that patients reply to the opener at all (variant analytics exist, data does not); the out-of-area lookup ("I'm in Florida") is not built; real reply and escalation rates; human minutes per patient; that the economics hold (all workload rates are assumptions); that this produces measurable completion lift |

## Layout

| Path | What |
|---|---|
| `ocp/rules.py` | Allowed actions: order state machine, eligibility (threshold + intended due), quiet/business hours, attempt caps, ceilings, pre-screen of untrusted text, field-by-field validation of model output |
| `ocp/engine.py` | Orchestrator: import → screen → cadence → inbound → escalation → verification; preferences; decision records; outbox with kinds/epochs/content versions; commit-before-call sends; sender-bound recovery |
| `ocp/feedback.py` | Kate's feedback loop: evidence capture, durable storage, task export (synthetic-only) |
| `ocp/templates.py` | Every sentence a patient can receive (GSM-7 only; policy caps at 3 segments, most render to 1–2) |
| `ocp/directory.py` | Verified partner directory; validity checked at the moment of use |
| `ocp/llm/` | `base` (closed intent schema, prompt, partner context, planner actions), `mock` (rule-based stand-in), `anthropic_adapter` (live), `router` (cheap→strong, shared budget), `composer` (fact sheet → words → fact check), `resolver` (version 4: menu resolver + independent reviewer; rules, Claude, OpenAI) |
| `ocp/geo.py`, `ocp/portal.py` | Version 4: geocoder interface + local centroids; portal messaging adapter (simulated) |
| `ocp/facts.py`, `ocp/referrals.py`, `ocp/scheduling.py`, `ocp/improve.py` | Version 5: fact cards + chart review; referral lifecycle + dashboard payloads; simulated scheduler + bookings; feedback → evaluation cases → candidate changes |
| `docs/partner-data-specification.md`, `data/schema/`, `data/examples/` | Version 5: the partner-facing data specification, JSON Schema, synthetic payloads |
| `docs/architecture-v5.md`, `docs/codex-review-packet-v5.md` | Version 5: architecture note with the assertions audit; the review packet |
| `ocp/messaging/` | `simulated`, `twilio` (disabled by default) |
| `ocp/server.py` | Dashboard + JSON API + Twilio webhook, 127.0.0.1 only, no auth (local prototype) |
| `ocp/scenarios.py` | 41 scripted scenarios (14 from round one, six round-two experiences, ten version 4, eleven version 5: the end-to-end journey, missing rationale, conflicting notes, role gating, plan change, ambiguous ownership, overdue referral, failed relay, duplicates + emergency follow-up, stale prep, unsupported claim; plus the version 4 list: emergency, portal link, prep, relay, resolver alternative, absolute constraint, mobile stop, out of area, location share, menu); `seed` uses the same cohort without scripting |
| `data/` | Synthetic directory, orders feed (`"synthetic": true`), open product decisions |
| `eval/` | Dev and held-out conversation cases, runner, live-eval instructions |
| `docs/` | Technical brief (canonical, dated); the Sept 15 decision-history brief; one-pager; architecture note; partner data specification |
| `feedback/exports/` | Exported tasks (git-ignored) |

## Hard rules the model cannot override

- STOP / HELP / wrong number are decided in code before any model call, win during pauses and holds, and
  suppress the **sender's** number; patient-level effects apply only if that is still the number of record.
- Model output is validated field by field against a closed vocabulary.  The model never decides scheduling facts,
  completion status or consent.  Its validated intent classifications influence escalation routing.  Version 3+: the model writes the words of a reply within the fact
  sheet; version 4: the resolver picks from an application-verified menu; emergency wording, compliance, portal
  handoff, prep answers and the relayed message are never model-written.
- Only sites and instructions in the verified directory (signed, unexpired) are offered; a content digest of
  each site/instruction a message depends on is re-checked at send, so any change cancels the queued text.
- A plan is a concrete date at a site open that day within every stated time constraint; contradictory bounds in
  one message get a clarifying question, a new bound that contradicts an old one withdraws the old one, and a
  plan the new constraints rule out is cleared and re-offered.  The confirmation says "walk-in plan, not a
  booked appointment."  No live availability is implied.
- `verified_complete` comes only from a partner result covering every line.  Patient-reported completion is
  `claimed_complete` with the reported location and in/out-of-network flag; a human may attest
  `completed_external` with an evidence note, which is reported separately from partner-verified.
- Held conversations (`waiting_partner`, `escalated`, `paused`) are human-owned; ordinary replies are recorded
  and annotated, not acted on.  Three exceptions, all bounded: a clinical question or request for clinical
  staff opens a clinician item (once) with the safety acknowledgement; a request for a real person opens a
  Kate item (once); the answer to a pending "our lab or elsewhere?" question updates the claim.  Scheduling
  holds are never lifted by these.  A partner result closes an order but never resolves a clinician's item.
- Sends: commit-before-call; a crash, timeout or 5xx after the call leaves an `ambiguous` row that is never
  resent; state, recipient and directory versions are re-read before every provider call; pending inbound is
  drained before any send.

## Templates versus constrained generation

Since version 3 the model writes the words of most replies within an application-owned fact sheet, checked by a
lexical allowlist and falling back to the approved template (see "What changed in version 3").  Compliance
confirmations, safety texts, the portal handoff, rationale answers, plan-change texts, booking confirmations and the
relayed message itself are approved wording only.  The history of this decision (D6) is in `docs/technical-brief-2026-09-15-decision-history.md`.

## Live model adapter and Twilio (optional)

> Live classification is unverified for this checkout following a recorded schema rejection on September 23, 2026.  Use mock mode for the walkthrough.  See the technical brief, section 6.

```bash
pip install anthropic && export ANTHROPIC_API_KEY=...     # or `ant auth login`
python3 -m ocp serve --model anthropic                      # Claude Opus 5, structured JSON output
python3 -m ocp serve --model routed                         # Haiku 4.5 first, Opus 5 fallback
```
Twilio sending requires `OCP_TWILIO_SEND_ENABLED=1`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and
`TWILIO_FROM_NUMBER` (or a messaging service SID) in the environment.  Nothing in this repo sets them.
Production must use Twilio's SDK validator for webhook signatures.

## Review record

The full Claude × Codex review record is internal and not included in this portfolio: round one (four
stages), post-cycle fixes, Codex's round-two verification, the round-two build, and the closing review.
