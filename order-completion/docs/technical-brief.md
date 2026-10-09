# StealthCo order-completion prototype: technical brief

*Brief dated **September 18, 2026** · describes **prototype version 5** · synthetic data only, no patient information.  This document is revised as the prototype changes; the revision history is at the end.  Contact: Kate Casaday.*


**Status words used below:** *functional* (implemented and tested) · *simulated* (a real interface with a stand-in behind it, labeled on screen) · *partner-dependent* (needs a decision or an integration from a health system) · *unverified* (a claim this build did not check) · *not built*.

## 1. What it is

A health system's clinicians write lab orders, and a share of them are never completed.  This prototype takes the orders that have sat unfilled past a threshold (45 days to start), runs a text conversation that gets the patient to the health system's own lab, and marks an order partner-verified complete only when the partner supplies results covering every outstanding line.  Human-attested external completion is recorded separately.  StealthCo operates no phlebotomists and no sites.  The prototype is Python with one SQLite file and runs locally with no credentials.

It was built with an AI coding agent (Claude Code), and every version was independently reviewed by a second model (OpenAI Codex) before the next one started.

How it got here:

| Version | What it added |
|---|---|
| 1–2 (Sept 15, 2026) | Order import, eligibility and consent rules, cadence, quiet hours, STOP / HELP / wrong number decided in code, a send protocol that never double-sends, the operator's work surface.  The model only classified intent; every sentence was a template. |
| 3 (Sept 15) | **The model writes the words; the application owns the facts.**  The engine builds a fact sheet, the model composes inside it, an allowlist fact check refuses any day, time, number, name, address, lab word or clinical/price word not on the sheet, and an approved template is the fallback.  Lab capability matching.  Opener variants with analytics.  A daily spend brake. |
| 4 (Sept 16) | Clinical handoff modes (portal link, portal relay, clinician queue).  Emergency wording decided in code: an immediate text telling the patient to call 911, with the nearest verified urgent care named as non-emergency care.  Approved prep answers.  For operational dead ends, the application builds a menu of verified options, a resolver picks one, and an independent reviewer agrees or escalates. |
| 5 (Sept 16) | A partner data specification in three tiers.  Fact cards from chart review with provenance, verbatim excerpts and role-gated approval.  Append-only order history (replaced, modified, attended, patient-reported change).  Referral records with a lifecycle.  Simulated booking.  An improvement loop: flag → versioned evaluation case → candidate change → release tag → rollback. |

## 2. Shape

```
partner data   (docs/partner-data-specification.md; data/schema/partner-data-v5.schema.json)
   orders feed · order events · care teams · directory · routes · policies
   clinical notes (record connection OR authorized manual review)
        │ importer     idempotent; append-only order events
        │ fact cards   notes → proposed cards → reviewed cards
        ▼
SQLite   patients · orders · order_lines · order_events · conversations · messages · events
         escalations · fact_cards · fact_reviews · clinical_notes · referrals · referral_events
         bookings · portal_messages · feedback · eval_cases · candidate_changes · release_history
         model_calls · human_time
        ▲
        │ the engine orchestrates · a rules module is the authority on what is allowed
        │   inbound  STOP / HELP / wrong number / emergency / menu digit / slot digit decided in code
        │            → held-state rules → usage and spend ceilings → classify → validate → dispatch
        │            → (resolver → reviewer) → template or composed text
        │   tick     cadence · reminders · verify deadlines · clinical follow-ups · referral lifecycle
        │            · outbox flush
        │
adapters (interchangeable)
   model       mock | Anthropic | routed · composer · resolver + reviewer: rules | Claude | OpenAI
   messaging   simulated | Twilio (send disabled)
   portal (simulated) · scheduling (simulated) · geography (local centroids)
```

**Division of labor.**  The application controls state changes and consent.  The model classifies replies into a closed vocabulary and can write wording inside an application-owned fact sheet.  An optional resolver selects from options built by the application, subject to review and execution checks.  Model classifications influence clinical routing; code screens (STOP, emergency wording, clinical keywords when the model is unavailable) and field-by-field validation constrain that routing.  The wording checker is **lexical, not semantic**.  Passing it does not prove that a message is clinically correct or semantically faithful.

## 3. What is functional, simulated, partner-dependent

| Area | Functional | Simulated | Partner-dependent |
|---|---|---|---|
| Partner data | Schema, three tiers, examples validated, importer for every event kind, capability flags enforced in the engine | Feeds are JSON files | Every interface; consent language; retention |
| Feed integrity (Sept 24, reconciled after review) | **Implemented and tested against adversarial cases:** every arriving payload validated, both streams (envelope, types, partner, sequence per stream, shape, rows and event payloads, enums, consent, duplicates by the importer's own identity, identifier re-keying on distinct patients, encoding, volume against the partner's own history, result coverage, test codes, timestamp offsets); a held file applies **nothing** (one transaction that also carries the per-patient holds and the receipt; schema creation kept out of it); on a stale feed every offer template, derived from the template registry, is withheld; bad rows quarantined, the rest imported, and a known patient touched by a quarantined row held from every non-safety message; the block enforced at the send boundary for every message except compliance, safety and the hold acknowledgement, whatever the pause string says; a replay of an already-accepted file is a no-op on state; late files (any delay, streams that never started) and a silent results stream noticed by us and closed by us on recovery; one message per incident to the partner's technical contact, identifiers only; the original payload retained so a rejected file can be inspected and its events stream reprocessed by a named operator (demographics are never re-applied from an old file) | The notifier (nothing leaves the process); the pickup (a local folder; runs when the scheduler tick runs, **nothing schedules itself**; at-least-once with no-op replay, not exactly-once) | Transport and credentials; the real contact and channel; thresholds from real files; whether an unreadable file of unknown kind should block (today it pages the partner and the 48 h stale timer backstops) |
| Fact cards | Versioned cards with provenance, verbatim-excerpt rule, conflict groups, role-gated approval (operator vs. clinical reviewer), supersession chain, chart-review minutes | The role switch; the rule extractor (a live extractor is an interface only) | Who the clinical reviewer is; record connection or manual review |
| Orders over time | Append-only events; replacement inherits eligibility; patient-reported change pauses and reconciles | — | Which events a partner can emit |
| Referrals | One record per handoff with lifecycle, routing basis, urgency, duplicates, overdue, next-day emergency task, gated resolution, audit | Partner evidence entered by hand; relay delivery | Routes, urgency criteria, portal write capability |
| Conversation | Rationale answered only from approved documented cards or an honest gap; emergency text; urgent care as non-emergency care | SMS transport (nothing leaves the process) | Whether relay is permitted at all; wording approval |
| Booking | Slots at bookable sites; book / walk-in / move / failure tracked separately | The scheduler | A scheduling integration |
| Improvement | Flags, versioned evaluation cases with machine checks, candidate changes, approval, release tag, rollback | — | Release process as the team grows |

The clinician handoff is a database row with a due time.  **Nobody is notified.**  How a partner's clinicians want to receive and close these items is the first thing a partner has to decide.

## 4. Evidence

- **Tests.**  The source contains 257 automated test methods.  The scenario tests exercise 41 scripted scenarios, including the version 5 end-to-end journey.  Run the commands in the README to check this checkout.
- **Live model runs, September 15, 2026, synthetic patients only:** intent classification agreed on 17 of 19 held-out replies on Claude Opus 5; wording evaluation 10 of 10 on Opus 5 and on Sonnet 5; 20 of 20 opening messages with no fact-check fallback.  The held-out set is 19 synthetic replies written by the same people who wrote the prompt.  It is not model accuracy on real patient language.
- **Prepared, not exercised:** the live resolver and reviewer (no paid model calls were made for versions 4 and 5); the OpenAI reviewer; the Twilio adapter (sending disabled; signature check not verified against a known-good vector).
- **Independent review:** the second model reviewed every version.  Its version 5 closing review found ten defects (capability flags stored but not enforced, a failed cancellation reading as success, and eight more); all were fixed the same day with regression tests.  The full review record is internal and not included.
- **Cost shape** (`costs/`; workload rates are labeled assumptions; model prices were read once on September 15 and not re-checked): model tokens are a rounding error at this workload.  SMS and human minutes dominate, and the operator's queue is the number that decides staffing.

## 5. Controls, and the kill switch (design placeholder: not built)

**What exists today (functional):**
- **Patient STOP.**  Decided in code before any model call, persistent, number-level.  It wins during a hold and during a pause.
- **Operator pause.**  One control halts all outreach and another lifts it.  The pause writes an audit event.  STOP confirmations and the emergency text still go out through it.
- **Stale-feed pause, per partner.**  If a partner's orders file or results file is more than 48 hours old, scheduled outreach and site offers are withheld at send time until a fresh pair arrives.
- **Feed-integrity block, per partner (Sept 24).**  A file that fails validation on arrival is held with nothing applied (one transaction that also carries the per-patient holds and the receipt itself, so a receipt says rejected only when nothing was committed); every message for that partner except compliance, safety and the hold acknowledgement stops at the send boundary (the acknowledgement still leaves under the automatic pause, not under a person's) and the scheduler pauses, until a newer good file of the same kind arrives or an operator clears the block with a named note.  A replay of an already-accepted file is a no-op on state and does not clear it; an older file arriving behind a good file that is still current is held and reported but does not block, and is never applied.  An unreadable file whose stream cannot be told pages the partner without blocking; the 48-hour stale pause is the backstop.  A patient whose own row was quarantined, for any reason, is held from every non-safety message until a clean row arrives.  Before the stale pause can trigger, a file that has not arrived when expected is reported to the partner's technical contact by us and closed by us when it lands.  Operating principle: never outsource to the health system's technology team work we can do ourselves.  Design record: an internal feed-integrity brief (not included).
- **Epochs.**  Every outbound message carries the conversation epoch it was queued under; a hold or closure bumps the epoch, so a message queued before the hold is cancelled at flush rather than sent.
- **Spend brake.**  A rolling 24-hour dollar cap; when reached, approved templates are sent and live classification routes to a person.

**What the one-page overview promises and this build does not have:** *"You can pause all outreach at any time."*  Today's pause is global and operator-only.  A partner cannot pause its own patients.  This is the next build (version 5.1), and it goes in before any partner sees a live demonstration.

**Design placeholder:**

| Question | Proposed answer |
|---|---|
| Scope | Per partner.  Pausing partner A does not touch partner B. |
| Who can pull it | A named partner contact, authenticated.  A phone call or email to the operator has the same effect and is logged with the partner contact as the requester. |
| Effect | All outbound for that partner's patients stops at send time, checked at enqueue and again at flush (the same two points where contact authorization is checked today).  Queued messages are cancelled through the epoch, not held for later. |
| What still goes out | STOP and HELP confirmations.  The emergency text: a patient who texts "chest pain" during a pause is still told to call 911.  The partner confirms this in writing. |
| Inbound during the pause | Recorded and screened.  Emergency and clinical signals still open their items.  Nothing else is answered. |
| Resume | Explicit, by the partner.  Nothing queued before the pause is sent afterward; conversations re-enter the cadence at the next tick. |
| Audit | Pause and resume events with actor, requester, reason, time and the count of messages cancelled, in the partner's audit export. |
| Tests to write | Pause blocks scheduled sends and replies for partner A and not B; safety text passes; queued offer cancelled; resume does not replay; a crash during a pause recovers paused. |
| Size | Small in the engine (a per-partner setting, the send-time check, the tests).  The partner-facing authentication is the real work and belongs to the pilot hosting task. |

## 6. Not proven

Real SMS delivery; any real partner feed; the clinician handoff beyond a database row; concurrency under real webhook load; identity verification beyond the partner's phone on file; English only; one time zone; the dashboard has no authentication (it binds to the local machine only).  Reply rates, escalation rates and human minutes per patient cannot be estimated from scripted fixtures.  Claims this build did not check are tracked in an assertions audit: vendor portal write APIs, model eligibility under a business associate agreement, the Census geocoder, and current prices.  This is a synthetic prototype, not a demonstrated secure clinical platform.

A saved September 23, 2026 evaluation records 55 live-classifier calls rejected by the API because the structured-output schema was too complex.  The September 15 results above describe an earlier run.  Current live-classifier operation has not been verified for this portfolio.  The local walkthrough uses a deterministic mock classifier.  The run log is not included.

## 7. Path to a supervised pilot

1. Partner feed transport (SFTP or HTTPS pickup, or a push to our endpoint), credentials, and a scheduled worker that runs the pickup and the tick unattended (the prototype's pickup runs only when the tick is invoked); the validation, quarantine, health, alerting and the message to the partner's technical contact are built on our side (Sept 24).  Identifier reconciliation depends on the partner's interface.
2. A labeled set of about 200 partner-approved sample replies; set the confidence floor from data, not from the 0.6 default.
3. Messaging go-live: 10DLC brand and campaign registration, the provider's own signature validator, delivery receipts so "sent" stops meaning "accepted," an opt-out audit export.
4. The partner-level kill switch (section 5).
5. Hosting that supports a business associate agreement, secrets in a vault, authentication on every surface, audit-log retention.
6. Security review, then a supervised cohort with the partner's clinician queue staffed by the partner and chart review done by an authorized operator.

## 8. Open questions

These are the questions we are working through with advisors and prospective partners.  Answers differ by health system.

1. **Feed.**  Can the partner produce a daily export of open orders and result status with stable identifiers and a consent flag, to a transport their job can write to?  Monitoring is ours: we validate every file on arrival, notice a file that did not arrive, and tell a named technical contact something specific.  What we still ask of their team is the smallest thing only they can do: correct an export or check that the job ran.  Who owns the clinician queue?
2. **Chart review access.**  For a pilot of about 200 patients, will a privacy office accept authorized manual chart review by a named operator, or does it have to be a record connection from day one?
3. **The fact-check boundary.**  It is an allowlist, and it is lexical.  Will a partner's security team accept that, or should the application render the facts itself and let the model write only the prose between them?
4. **Kill switch.**  Should the emergency text pass through a partner pause (section 5)?  What is the smallest partner-facing control acceptable at pilot: a signed link, a call to the operator, a page behind the partner's single sign-on?
5. **Model-side HIPAA path.**  Zero data retention, or a HIPAA-configured organization with 30-day retention?  Which does a health-system compliance team expect, and how long does the agreement take?  (Our notes on current vendor terms are unverified.)
6. **SMS.**  The messaging provider under a business associate agreement, 10DLC registration for healthcare, and TCPA consent for the first text: does consent captured at the provider's intake cover a message sent on the provider's behalf, and who is the sender of record?
7. **Hosting.**  The smallest HIPAA-eligible footprint for a pilot, and how keys and feed credentials are held.
8. **Opening-message analytics.**  What de-identification and consent posture lets analytics on opening-message variants persist across partners?

## Revision history

| Date | Prototype version | What changed in this brief |
|---|---|---|
| Sept 18, 2026 | 5 | First general edition.  Current through version 5; adds the kill-switch design placeholder. |
| Sept 23, 2026 | 5 + feed integrity | Feed integrity built and reconciled after Codex's first review the same day: validation on arrival of both streams, atomic import, quarantine with per-patient holds, held files, the per-partner integrity block enforced at the send boundary (§5), late-file and silent-results detection with recovery, retained payloads, messages to the partner's technical contact.  §3 row, §7 item 1 and §8 question 1 revised.  Kill switch still not built. |

*Published for portfolio review.  See [NOTICE.md](../../NOTICE.md).*
