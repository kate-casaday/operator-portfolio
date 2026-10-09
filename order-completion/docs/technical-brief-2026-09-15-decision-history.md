# Order-completion prototype — technical brief (Sept 15, 2026; decision history)

> **Superseded Sept 18, 2026** by `technical-brief.md` – the canonical, audience-neutral technical brief.  Kept here as the decision history of versions 1–3 (the D-numbered decisions referenced elsewhere).

*StealthCo · September 15, 2026 · v0.3 (round two) · Author: Claude Code (Point) · Independent review: Codex (see the internal Claude × Codex review record (not included in this portfolio))*

**What this is.**  A runnable, credential-free prototype of the "stale order → text outreach → verified
completion" loop you sketched on September 11 ("Claude Code and Twilio"), now with Kate's work surface: she
plays a synthetic patient, flags a response, says what should have happened, and exports a development task
that carries the captured evidence.  Design constraint from Kate: *Kate and an algorithm* — maximize reliable
automated assistance, explicit human escalation, measured human workload.  Synthetic data only.  Stdlib
Python, one SQLite file.  Location: `prototype/order-completion/` (README has exact commands and the
five-minute walkthrough).

**Status vocabulary used below:** *implemented and tested* · *simulated* (labeled on screen) · *prepared but
not exercised* · *partner-dependent* · *hypothesis*.

## 1. Architecture

```
partner feed (orders ≥45d, result/cancel updates)         verified directory (sites, hours, link, instructions)
        │ importer.py  (idempotent, line-level)                     │ directory.py (refuses stale verification)
        ▼                                                           ▼
   SQLite: patients · orders · order_lines · conversations · messages(outbox) · events(audit) · escalations ·
           feed_imports · model_calls · human_time · settings(sim clock, pause)
        ▲                                                           ▲
        │ engine.py — the only writer.  rules.py — the only authority on what is allowed.
        │   tick():   outreach cadence, reminders, verify deadlines, stale-feed pause, outbox flush w/ retries
        │   inbound(): prescreen (STOP/HELP/wrong-number in code) → usage ceiling → model classify (bounded
        │              retries, router fallback) → validate to closed vocabulary → dispatch → template reply
        │
   llm/ adapters (mock | anthropic | routed)          messaging/ adapters (simulated | twilio, send disabled)
   server.py: dashboard + JSON API + /webhook/twilio, 127.0.0.1 only
```

**Division of labor.**  Code decides every action and every state change.  The model answers one narrow
question per inbound message: which of 14 intents is this, with what confidence and which stated
constraints (after-time, weekday, site choice).  Its answer is validated against a closed schema; anything
else becomes `unclear`.  The model never composes patient-facing text; every outbound sentence lives in
`templates.py` and placeholders fill only from the partner directory and feed.  That is what makes the
"only verified links, locations, hours, approved instructions" guarantee a property of the code rather than
of the prompt.

**Verification.**  `verified_complete` is reachable from exactly one place: a partner `result_finalized`
update with an explicit, non-empty list of known test codes covering every outstanding order line.  Empty
line lists, unknown codes, zero-line orders, future-dated files and replayed events are rejected and logged.
Link clicks are logged as signals.  "I already did it" moves the order to `claimed_complete`, holds the
conversation, and opens a reconciliation item for Kate; when the partner feed later confirms **every** claimed
order for that patient, the item auto-resolves at zero human minutes.

**Holds and epochs.**  `waiting_partner`, `escalated` and `paused` are human-owned states: ordinary replies
are recorded and appended to the open item, never acted on; STOP still wins.  Every outbound carries the
conversation epoch it was queued under; a hold, a plan change or a closure bumps the epoch, so a reply or
reminder queued before the hold is cancelled at flush rather than sent.

**Send protocol.**  Commit-before-call: a message is marked `sending` and committed before the provider is
called.  If the process dies after the provider accepted, the row is found in `sending` on restart, marked
`ambiguous`, never resent automatically, and a reconciliation item opens.  Simulated transport IDs are UUIDs.

**Round-two additions (implemented and tested).**  *Preferences*: every explicitly stated constraint
(after/before time, weekday, weekend/evening, town, caregiver, reminder frequency) is persisted with its
source (`model_interpretation` of the patient's own message, linked by message id; or `kate`), and a changed
value supersedes the old one with `corrected=1`.  The model sees the active preferences and the partner's
verified towns and site names, nothing else about the patient.  *Decisions*: every outbound carries a JSON
decision record (rule, intent, confidence, effective constraints, sites considered, reason) and every
conversation carries a `next_action_reason`; both are visible in the thread.  *Threshold*: the overdue
threshold is configurable 15–45 days (`/api/threshold`), an order with a future `intended_due_at` is never
overdue, and re-screening only ever promotes `ineligible` orders.  *Claims*: patient-reported completion
records a location and an in/out-of-network flag; a human may attest `completed_external` with an evidence
note (reported separately from partner-verified).  *Handoff status*: `queued_simulated` → `accepted` on
acknowledge; `notified` exists in the vocabulary but nothing can set it because no notification integration
exists.  *Feedback*: `ocp/feedback.py` captures evidence, stores Kate's interpretation separately, exports a
task, and tracks `open → linked → verified | dismissed`.

## 2. Interfaces

| Interface | Shape | Status |
|---|---|---|
| Orders feed | JSON: `{partner_id, generated_at, orders:[{source_order_id, patient{...}, ordered_at, priority, lines[]}]}` | Synthetic; a real partner will be SFTP/CSV or FHIR `ServiceRequest` — the adapter is the first live-integration task |
| Updates feed | `{partner_id, generated_at, updates:[{source_order_id, kind: result_finalized\|cancelled, at, lines[]}]}` | Same; real source is LIS/EHR result status (`DiagnosticReport`) |
| Directory | `data/partner_directory.json` with `verified_at`/`verified_by` per site; > 90 days → refused | Partner-owned in production |
| Model adapter | `classify(context, patient_text) -> ModelResult{intent, confidence, barrier, constraints, usage, latency, simulated}` | `mock` (default), `anthropic` (Claude Opus 5, JSON schema output, cached system prefix), `routed` (Haiku 4.5 → Opus 5) |
| Messaging adapter | `send(to, body, dedupe_key) -> SendResult`; inbound via `/webhook/twilio` (form POST, HMAC check) | `simulated` (default), `twilio` (REST over urllib; sending requires `OCP_TWILIO_SEND_ENABLED=1` + env credentials) |
| Ops API | `/api/state`, `/api/pause`, `/api/resume`, `/api/tick`, `/api/advance`, `/api/sim/inbound`, `/api/escalations/{id}/acknowledge|resolve`, `/api/import/updates`, `/api/link_click`, `/api/human_time`, `/api/threshold`, `/api/preference`, `/api/feedback[...]`, `/api/costs`, `/api/decisions` | Working; exercised by `tests/test_server.py` |

## 3. Explicit rules (the part that matters for a partner conversation)

- Eligibility: age ≥ threshold (default 45 days; configurable 15–45; changes affect future screening and
  promote `ineligible` orders, never retract eligible ones), `routine` priority only, no future intended due
  date, phone on file, partner-recorded SMS consent, no local
  opt-out, number not suppressed, number not shared with another patient (shared numbers are quarantined:
  no outreach, no model call, one identity item for Kate; STOP from that number suppresses everyone on it).
- Contact authorization is re-checked at enqueue and at send.  A never-contacted (ineligible) patient who
  texts gets no reply, but STOP is honored at number level.
- Cadence: initial + 3 follow-ups at 3-day intervals; then `unresolved`, never completed.  Quiet hours
  20:00–08:00 hold *scheduled* sends only; replies to a patient's own message go out.  A plan is a concrete
  date (next occurrence of the stated weekday on which the chosen site is open; otherwise the engine re-offers
  or asks for a day); the reminder goes at 17:00 the day before; 5 days after the visit date with no partner
  result → back to conversation.
- Suppression: `STOP`, `HELP`, "wrong number" decided by regex before any model call; suppression is
  persistent; the confirmation is the one message allowed through a pause.  Wrong number also clears the
  phone.
- Ceilings per conversation: 20 model attempts (one budget shared by engine retries and router fallback,
  every attempt recorded), 60k tokens (cache reads included), 12 outbound (queued messages count as reserved,
  enforced at enqueue), 40 inbound, 3 HELP replies → escalation.
- Model: bounded attempts then `provider_failure` escalation with a generic handoff text; router adds
  cheap→strong fallback on error or confidence < 0.6.  SMS: 3 attempts then `sms_delivery_failed`.
- Feeds: per partner, both the orders file and the results file must be < 48 h old.  Otherwise scheduled
  outreach and site offers are withheld at send time (a reply gets a "confirming your order is still open"
  acknowledgement) and the next tick records `policy:stale_feed`; a fresh pair of files resumes.
- Clinical questions → partner clinician queue item with an assignee (from the directory), a due time (4
  business hours from the next clinician window) and an acknowledge action; overdue items are flagged on the
  next tick.  **This handoff is simulated: a database row, not a page or a message to anyone.**  Business
  hours (Mon–Fri 08–17): "logged for the nurse line; expect a reply today" + clinic phone.  After hours:
  next-business-day language + clinic phone + 911, sent immediately regardless of quiet hours or a Kate pause.
  The conversation is held until the clinician resolves; a partner result closes the order but leaves the
  clinician's item open with a note.  Cost questions → Kate queue, no price ever quoted by text.
- Isolation: the model context is this patient's thread, open test names, active preferences and the partner's
  verified towns/site names; no other patient's data, no source ids, no phone numbers.  Instruction-like inbound
  text is flagged in the audit trail and treated as data.  No sensitive profile is inferred or stored: the
  preference keys are a closed list.
- Plans: a plan is a concrete date at a verified site open that weekday within every stated time constraint
  ("evenings or weekends" is treated as OR: a weekend site closing at noon still qualifies).  If the named or
  chosen site is infeasible the engine re-offers within the constraints or opens a Kate item.  The confirmation
  text says "walk-in plan, not a booked appointment"; no live availability or booking is implied.
- Requests for clinical staff go to the partner clinician queue (`clinical_staff_request`); requests for a real
  person go to Kate's queue (`human_request`); "fewer reminders" sets a preference (one more reminder in 7
  days, then unresolved) and is never treated as opt-out.

## 4. Test evidence — what actually ran (September 15, 2026, Python 3.9.6, macOS)

`python3 -m unittest discover -s tests -t .` → **all tests passing** (count printed by the run; 44 at v0.1, 107 at
the start of round two's closing review, more after the closing-review fixes in `tests/test_closing.py`).
Codex's own runs pass everything except the HTTP tests (`tests/test_server.py`), which its sandbox's socket
policy blocks.  `python3 -m ocp demo` → **20/20 scripted scenarios** (14 from round one + caregiver-nearby,
working-parent evening/weekend, combined-constraints-then-correction, completed-elsewhere-out-of-network with
human attestation, clinical-staff request with accepted handoff, fewer-reminders-then-silence).  The demo ends
with every scripted patient closed or held; `python3 -m ocp seed` (or the dashboard's *Fresh synthetic session*)
is the entry point that leaves 20 interactive patients.

Covered: ordinary completion with a concrete plan date, day-before reminder and verify deadline from the
visit date; plan without a day → ask; plan on a closed day → re-offer; ineligible screening; link click and
attendance claim ≠ completion; replies during a clinical hold are recorded, not acted on, and a partner result
does not resolve the clinician's item; STOP wins during a hold and during a pause; clinician due time,
overdue flag, acknowledge, partner minutes reported separately; after-hours ack sent at 21:00 despite quiet
hours; quiet hours hold scheduled outreach but not replies; Kate pause holds replies but not compliance or
safety texts; **crash after provider accept → ambiguous row on restart, never resent, item opened**; restart
against an existing database → no ID collision, no resend; simulated inbound without IDs are distinct;
replayed inbound id; replayed outbox trigger; re-imported feed and replayed result event; shared phone →
quarantine, STOP suppresses everyone; unconsented patient gets no reply, STOP honored; partner consent
withdrawal stops sends; local opt-out survives re-import; max attempts → unresolved; stale feed withholds an
offer with no tick, other partner's file does not count, future-dated file rejected; provider failure with
every attempt recorded; router records error/discarded/fallback attempts and respects the shared budget;
SMS failure → retry → escalation; outbound ceiling enforced at enqueue including queued; HELP capped;
low-confidence downgrade; junk and malformed model output cannot move state or crash; software error
mid-processing → contained, handoff, replay is duplicate; claim before first outreach; partner cancellation
cancels the reminder; queued offer cancelled when a claim arrives before send; reschedule cancels queued
plan text; partial panel; empty/unknown result lines and zero-line orders rejected; new order after closure
reopens the conversation; claim item stays open until every claimed order is verified; directory validity at
time of use (100 days later → no site, item opened); schedule constraints only matching verified sites,
combined constraints; cost question never quotes a price; resolution records logged vs assumed minutes; "I
can stop by Friday" is not an opt-out end to end; cross-patient context isolation (spy adapter);
prompt-injection text → approved template, no leakage, flagged; model-claimed plan without a prior offer is
not honored; unknown number gets no reply; HTTP API flow incl. acknowledge; Twilio disabled-by-default and
signature self-consistency.  Post-verification (`test_postcycle.py`): a reopened conversation sends a genuinely
new outreach (new episode keys) and cannot escape an open clinician item; a queued text is cancelled when the
partner changes the patient's number; a STOP committed but unprocessed before a crash is recovered on restart
or at the next tick; state is re-read before each send in the same batch; clinician deadlines count business
hours (Friday 16:30 → Monday 11:30) and acknowledgement does not stop the overdue check; a result arriving
before its order applies on replay; Twilio timeouts/5xx are ambiguous (never retried) while 4xx/refused
connections are retried; the outbound cap holds when the model cap is also hit; weekend+time constraints
intersect; a queued offer whose directory entry expired is cancelled at send; the live adapter records real
token usage on refusal and bad-JSON paths (fake client, no network).

**Not tested / not proven:** any live model call (no credentials in this environment; the live adapter is
exercised only with a fake client); real SMS delivery (no delivery receipts; "sent" = provider accepted); the
clinician handoff beyond a database row (`accepted` means someone clicked accept in the dashboard); Twilio
signature against a known-good vector; concurrency under real webhook load (global lock); real partner feed
formats; real reply and escalation rates; template wording with a partner's compliance team;
accessibility/language (English only); time zones (single local clock); identity verification beyond the
partner's phone-on-file; the dashboard has no authentication (127.0.0.1 only).

## 5. Instrumentation and cost

Every model *attempt* (successful, failed, discarded low-confidence, fallback) records adapter, model,
`simulated` flag, tokens (input/output/cache-read; unknown for failed attempts), latency and outcome.  Every
outbound records segments (GSM-7 / UTF-16 units); inbound segments are counted on the original length.  Human
minutes are recorded when Kate resolves an escalation (a logged figure, or a per-reason default explicitly
labeled `default_assumed`) or logs oversight time; clinician minutes are the partner's and reported
separately.  `python3 -m ocp metrics` prints the roll-up; the dashboard shows the same numbers and labels
simulated attempts and provider-accepted (not delivered) messages.

`costs/cost_calculator.py` (prices in `costs/prices.json`, each with source and read date) — per month,
recurring, excluding founder comp:

| Patients | A. Opus 5 API (uncached) | B. Haiku→Opus | C. Self-host DeepSeek-V4-Flash | Human hours (Kate queue + oversight) |
|---:|---:|---:|---:|---:|
| 100 | $129 | $129 | $11,738 | 11.7 |
| 1,000 | $203 | $199 | $11,805 | 26.7 |
| 10,000 | $946 | $903 | $12,477 | 176.7 |

Model tokens are a rounding error at this workload ($0.78 → $78/month on Opus 5, modeled uncached because
the prototype's ~350-token system prefix is below every current model's minimum cacheable size); SMS and
human time dominate.  Option C is modeled as 2×H200 (or 4×H100) running 24/7 — a chosen serving scenario,
not a demonstrated hardware minimum — plus a quarter-time MLOps engineer; at these volumes GPU utilization
would be under 1% (against an assumed, unbenchmarked capacity).  It is not cheaper at any scale modeled and
raises a BAA question the GPU renters were not asked.  Option B saves ~$43/month at 10k patients and adds a
second model to evaluate; not worth it until token volume is 100× higher.  All workload rates are labeled
assumptions; the demo's ratios come from scripted fixtures.  One-time development to reach a live partner:
~$115k (engineer months for feed adapter, messaging go-live, live-model evaluation; security and legal
allowances) — see `costs/results.md`.

**Round-two pressure test (`costs/workload_scenarios.py`, editable JSON).**  The baseline above assumes short
classification calls and does not price a longer conversation.  Low / medium / high conversational scenarios
(messages, segments, calls incl. retries and routing, context/output length, escalation rates and minutes,
partner clinician time) at 10,000 patients put patient-service inference at $75 / $488 / $2,231 per month on
Opus 5 and SMS at $622 / $1,260 / $2,529; Kate's queue at 77 / 260 / 610 hours per month, which is the number
that decides staffing, not tokens.  The report also shows the maximum Kate-queue escalation rate that fits a
10 / 20 / 40 / 80-hour monthly budget at each scale (at 10,000 patients, 80 hours absorbs about 7% in the
medium scenario), and prints unresolved patients next to the "no human queue" rate so silence cannot read as
success.  Appropriate clinical escalation is required, not a cost to minimize.  The self-hosted line is one
scenario (DeepSeek-V4-Flash on 2×H200, 24/7, 0.25 FTE MLOps) with its hardware, utilization and staffing
assumptions labeled; it is not a statement about open-weight models generally.  Cost categories are separated:
inference, SMS + infrastructure, Kate's time, partner clinician time, development-agent costs (unmetered;
placeholder), one-time integration, founder compensation.  The ~$115K setup estimate is reassessed task by
task there: engine/rules done in synthetic form; feed adapter and clinician-queue integration
partner-dependent; security and legal lines need outside quotes; clinician-queue integration and identity
verification were missing from the original figure.

## Templates versus constrained generation

Every patient-facing sentence is a template with placeholders filled only from the verified directory and the
partner feed.  Where this constrains the experience: acknowledgements are uniform ("Got it - Friday Oct 2 at
…"), a patient who writes three sentences gets one shaped reply, and mixed intents (a question plus a day)
answer only the dominant intent.  What constrained generation could add: a one-sentence acknowledgement that
mirrors the patient's wording, in the patient's language, inside a fixed frame that still carries the
template's facts.  What it must never own: scheduling facts, hours, links, completion status, consent, and
escalation — those stay in code.  Recommendation: keep templates until a labeled evaluation on partner-approved
samples exists; then trial a single bounded slot (the acknowledgement clause) with the live adapter, gated by a
regex/length check and the same segment limit, and compare flag rates in Kate's feedback loop.  This is open
decision D6 on the dashboard.

## 6. Unknowns (ranked)

1. **Partner data access.**  Everything depends on a daily (or better) export of open orders and result
   status with stable identifiers.  Format, cadence, consent flag, and who signs the BAA are unknown.
2. **Live intent accuracy.**  The mock is regex.  A labeled set of ~200 partner-approved sample replies
   is the first evaluation task; the escalation precision/recall numbers decide how much of the queue
   Kate actually sees.
3. **Reply rate and escalation rate.**  Human-minutes per patient is the number that sets staffing; the
   demo cannot estimate it.
4. **Compliance wording.**  Templates, consent basis (treatment communication vs marketing), 10DLC
   registration, and the after-hours clinical language need partner and counsel sign-off.
5. **Identity.**  The prototype trusts the partner's phone→patient mapping (and quarantines numbers shared by
   more than one patient).  A real deployment needs a verification step before any order detail is texted
   (v0.2 texts only "lab work ordered by Dr. X").
6. **Clinician handoff.**  v0.2 models the clinician queue as rows with assignee, due time, acknowledge and
   overdue flags.  Nothing notifies anyone.  The partner must say how its clinicians want to receive and
   close these items before a patient is told "we've logged it for the nurse line."

## 7. Work to reach a live partner (sequence)

1. Partner feed adapter + identifier reconciliation (1–1.5 engineer-months; blocked on partner interface).
2. Replace mock with `anthropic` adapter; build the labeled reply set; measure; set the confidence floor
   from data, not from the 0.6 default.
3. Twilio go-live: 10DLC brand/campaign, webhook hardening (use Twilio's SDK validator), delivery
   receipts (status callbacks → `delivered`/`undelivered`, so "sent" stops meaning "accepted"), opt-out
   audit export for the partner, reconciliation procedure for `ambiguous` rows.
4. Hosting on a BAA-capable cloud; secrets in a vault; audit-log retention; access control on the
   dashboard (v0.1 has none — it binds to localhost only).
5. Security review (Sara Lazarus allowance in the pro forma), then a supervised cohort with the
   clinician queue staffed by the partner.

## 8. What a live partner still needs (partner-dependent, in order)

Daily export of open orders, results and cancellations with stable identifiers and a consent flag (format
decides the adapter's size); a clinician-queue delivery mechanism and a named owner; the overdue threshold
and intended-due-date semantics for their order mix; template and consent-language approval; identity
policy before order detail; BAA-capable hosting.

## 9. Questions for a health-system advisor (as sent)

1. Which of the two candidate partners (the FQHC or the 350-provider system) can produce a daily open-order
   and result-status export with a consent flag, and who there owns the clinician queue?
2. Is a template-only patient channel acceptable to a partner's compliance team as the v1, with model
   free-text held back until an evaluation set exists — or will the partner want free-text from day one?
3. What is the identity-verification bar before an order detail is texted: partner phone-on-file only,
   or a challenge (date of birth) first?

*Added September 15, 2026 (version 3 planning; Claude's questions, Kate's list).*

4. **Surface choice.**  Version 3 calls Claude directly through the Anthropic Python SDK (Messages API with
   structured outputs), one request per patient message, with the application owning eligibility, site
   selection, fact checking, holds and escalation.  We chose this over a Console-created Managed Agent because
   the guardrails and the outstanding-order data model are the company's asset and must live in our code, and
   because a per-SMS reply with a hard fact check is a workflow, not an open-ended agent.  Anything you would
   change before a partner's security team sees it?
5. **HIPAA path on the model side.**  Anthropic offers a BAA on the first-party API in two configurations:
   zero data retention for qualified accounts, or a "HIPAA-configured" organization with 30-day retention that
   covers the Messages API features we use (prompt caching, structured outputs).  Which would a health-system
   compliance team expect, and have you seen the timeline for getting a BAA signed with Anthropic?  Until then
   the prototype runs on synthetic patients only.
6. **SMS provider.**  Twilio (or an alternative) under a BAA, 10DLC brand and campaign registration for
   healthcare messaging, and TCPA prior-express-consent for the first text: does consent captured at the
   provider's intake cover a message sent by us on the provider's behalf, and who is the sender of record?
7. **Secrets and hosting for a pilot.**  The prototype keeps a workspace-scoped API key in the shell environment
   only.  For a pilot on real data, what is the smallest HIPAA-eligible footprint you would stand up (your
   earlier Azure/Postgres budget line), and how should keys and the partner feed credentials be held?
8. **The opener as a measured asset.**  We will record which first-text variant each conversation received and
   report reply, plan and completion rates by variant.  What de-identification and consent posture lets us keep
   that analytics dataset across partners as a company asset rather than partner-owned data?
9. **Out-of-area lookup.**  A patient replies "I'm in Florida, is there a place near me?"  Is there a legitimate
   national lab-locator data source (Quest, LabCorp, or the partner's reference-lab network) we can query, and
   can a partner's order be drawn at a site outside its network under the partner's arrangement?

10. **Pressure-test the $130K (added Sept 17, 2026).**  `finance/cost-to-build-and-volume-tiers-2026-09.md` puts the one-time
    technology to a first supervised pilot at about $130K on top of version 5: partner feed adapter and identifier reconciliation
    (1.5 engineer-months), messaging go-live (0.5), live-model evaluation from partner samples (1.0), version-3 leftovers (0.5),
    a security-review allowance ($40.5K), legal/BAAs ($15K), hosting ($5K), at $20K per loaded engineer-month.  Your Sept 11
    view was that one dedicated engineer is enough for this phase.  Two questions: (a) which of these lines would you cut, and
    which would you double, having run an agentic SDLC on a real partner integration?  (b) is the $2M / 18-month frame
    (two founders, one engineer, two systems) the right shape, or does a $500K–$750K, one-system, prove-it-first round get to
    the same evidence?  Kate's stated intent (Sept 17): this is the discrete gap-closure business or nothing; she needs capital
    committed or a clear "no" quickly.

## 10. Version 3 (September 15, 2026): the model writes the words; the application owns the facts

**What changed.**  Rounds 1–2 let the model choose an approved template through intent classification.  Version 3
adds a second model role, the **composer**: the engine decides the action and selects the verified facts exactly
as before, builds a **fact sheet** (sender line, first name, provider's office, visit date when the feed has it,
test *category*, patient town, the capable sites with hours and the hours relevant to what the patient said, the
approved link and instructions, the plan, which facts the thread has already carried, the patient's own words,
and an `ACTION_GUIDE` entry saying what this message must carry and whether it may end in a question) and asks the
composer for the text.  `fact_check` (`ocp/llm/composer.py`) refuses any text carrying a number, time, address,
URL, phone, lab name, provider name, town or test name not on the sheet, banned booking/guilt language, a
question where the action is a statement, or more than three segments.  A refused text is retried once with the
violations listed, then the approved template is sent and the refusal is recorded as an event and in the message's
decision record.  Compliance and safety acknowledgements are never composed.  Two composers exist: a deterministic
**fact writer** (tests, demo; follows Kate's opener structure and acknowledges changed constraints) and
**Claude Opus 5** through the SDK with structured output at effort `low`; the live composer is shown the fact
writer's rendering as its example when the fact writer has a structure for the action.

**Lab capability matching.**  `data/service_catalog.json` maps test codes to required collection services, a
plain-language category, and a sensitivity flag.  Sites carry `services` and optional `service_hours` (a timed
glucose tolerance test must start by 10:00 at Brunswick).  `Directory.nearest` / `filter` accept requirements;
`Engine._requirements` derives them from the patient's outstanding lines; plan confirmation rejects an incapable
site and re-offers a capable one.  Sensitive tests are never named; any test name is withheld until the patient's
first reply.

**Opener as a measured asset.**  Every conversation records `opener_variant` (structure `kate-v3`, disclosure
`none`/`short` alternating by conversation id by default, sites named, visit date present, writer).  The
Operations page reports sent, replied, reply rate, median hours to first reply, plan rate and completion rate by
variant.  Disclosure mode and site count are settings (`POST /api/settings`).

**Spend brake.**  `Policy.max_spend_usd_per_day` (default $5) over a rolling 24 wall-clock hours, computed from
`model_calls.cost_usd` priced per attempt at the attempt's own model.  When reached, live composition is skipped
(approved template) and live classification routes the reply to a person with a `spend_cap` escalation.
Simulated writers are not model calls and cost nothing.

**Interface.**  `/` Operations; `/conversation/<id>` one phone-like thread with Flag and "why"; `/legacy` the
round-two dashboard.  Stdlib HTTP, 127.0.0.1 only, as before.

**Evidence (Sept 15).**  149 tests pass under Python 3.11 (`tests/test_v3.py` adds 18: capability matching,
opener structure and variants, feedback #1 acceptance, fact-check refusals and template fallback with a scripted
live composer, provider failure, compliance/safety never composed, spend brake, Operations payload, request
construction).  Demo 20/20.  Wording evaluation: fact writer 10/10; **Claude Opus 5 10/10** ($0.17) and
**Claude Sonnet 5 10/10** ($0.07) with the final prompt after two rounds of prompt tightening documented in
`eval/README.md`; **20/20 openers** on Opus 5 with no fact-check fallback ($0.29).  Live held-out classification
**17/19** on Opus 5 ($0.05).  Total live spend for the build: about $2.20, all in Kate's `Chief of Health Demo`
workspace.

**Known limits.**  The out-of-area lookup ("I'm in Florida") is not built; the reply is the existing
no-site/coordinator path.  Disclosure wording is a setting, not yet negotiated with a partner.  The wording set is
ten synthetic threads and the checker is lexical: it proves facts and structure, not tone.  `ho-01`/`ho-15`
(site + day phrased as willingness) are the next classification fix.  The Codex closing review of version 3 has
not yet run at the time of writing.

**Reconciliation (same evening).**  Codex's closing review found the fact check to be a partial denylist and the spend cap
to drop clinical routing.  Both fixed: `fact_check` is now an allowlist over days, times, numbers, proper nouns, addresses,
lab words, provider mentions and clinical/price vocabulary, with day-and-time coherence against the named site's hours and
per-action commitment anchors; every model-unavailable exit (usage ceiling, spend cap, provider failure) runs the same
keyword screen so a nurse request still reaches the clinician queue.  Unknown test codes fail closed; openers carry send
dependencies; variant analytics count sent exposures only.  162 tests; live 10/10 on both models and 20/20 openers against
the stricter checker.  **The boundary is lexical, not semantic.**  Question 10 for you: would a partner's security team
accept this, or should version 4 move to application-rendered fact cards with the model writing only the prose between them?
