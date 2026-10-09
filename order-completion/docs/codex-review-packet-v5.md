# Codex review packet — Chief of Health version 5 (Sept 16, 2026)

*For the Red Team's closing review under `ops/dual-model-protocol.md`.  Point: Claude Code.  Everything synthetic; no credentials, no paid calls, no network.*

## What to read, in order

1. the internal v5 brief (not included) — Kate's direction and the design in one page, including the **assertions audit**.
2. `prototype/order-completion/docs/partner-data-specification.md` + `data/schema/partner-data-v5.schema.json` + `data/examples/*.json`.
3. `prototype/order-completion/docs/architecture-v5.md` — functional / simulated / partner-dependent / unverified, per area.
4. `prototype/order-completion/README.md` — "What changed in version 5", commands, the demo walkthrough.
5. The last section of the internal review record (not included) ("Version 5 build") — what ran, test changes, and Point's asks.

## Code map (new in v5)

| File | What | Where it is exercised |
|---|---|---|
| `ocp/facts.py` | fact cards, notes import, rule extraction (quotation), role-gated review, supersession, rationale for replies, payloads | `tests/test_v5.py::V5_FactCards`, scenarios 31–34 |
| `ocp/referrals.py` | referral records, routing by care team + routes, urgency policy, lifecycle transitions, evidence, resolution gating, tick (overdue / expiry / next-day emergency task), payloads | `V5_Referrals`, scenarios 31, 32, 36–39 |
| `ocp/scheduling.py` | simulated scheduler; bookings ledger; funnel | `V5_Booking`, scenario 31 |
| `ocp/improve.py` | feedback on message / card / referral with category; evaluation cases (versioned, replayable); candidate changes with evaluation, approval, release, rollback | `V5_Improve`, scenario 31 |
| `ocp/importer.py` (+`order_events`) | `replaced` / `modified` / `attended` / append-only history; care team on orders | `V5_LongitudinalOrders`, scenarios 31, 35, 39 |
| `ocp/engine.py` | rationale replies, plan-change pause + reconciliation, replacement inherits eligibility, reason-specific follow-ups, referral creation at every handoff, booking flow, emergency wording change, named-site confirmation | all of the above |
| `ocp/server.py` | `/referrals`, `/facts`, `/improve` pages + APIs; v5 Operations section; role setting | `V5_Surface`; HTTP smoke in the record |

## Point's asks (please reproduce, not just read)

1. **Fact boundary of the rationale reply.**  Can any path put a non-verbatim or unapproved statement into `rationale_documented`?  Can an operator get a clinical card to `approved` by any sequence (edit → approve, supersede, conflict resolution)?  Can an unresolved card reach a patient?
2. **Reconciliation.**  Can a patient text change an order's lines, state or target?  Does a `replaced` event ever leave both orders open, or close the conversation wrongly?  Does a result for the replaced order close the replacement?  Duplicate / out-of-order events.
3. **Referral lifecycle.**  Any handoff that creates no referral?  Any path to `resolved` without a basis, or on a clinical kind without authority or evidence?  Does "sent" ever display as delivered?  Duplicate detection and overdue arithmetic (business hours).
4. **Emergency.**  The 911 text never waits; the next-day task is separate; urgent care is never presented as the emergency channel; idioms.
5. **Booking.**  Can a walk-in intention be shown as booked?  Slot digit vs menu digit collisions (`pending_slots` vs `unclear_menu`).  MOVE / failure / reschedule paths; attendance never equals completion.
6. **Improvement loop.**  Can a free-text expectation pass?  Can a candidate be released without evaluation or approval?  Are interaction logs, cases and (nonexistent) training data actually separate?
7. **Measures.**  Are "missed" and "unnecessary" escalation computed only from labeled checks, and never presented as real-patient rates?
8. **Test pins.**  `walkin_policy()` and `v3_policy()` pins: is any v5 default left untested?
9. **Assertions audit.**  Anything in the briefs or README still stated as fact that this build did not verify.

## How to run

```bash
cd prototype/order-completion
python3.11 -m unittest discover -s tests -t .      # expected: all pass (count in the README)
python3.11 -m ocp demo --quiet                     # expected: 41/41
python3.11 -m ocp serve                            # then /, /referrals, /facts, /improve, /conversation/<id>
```

Snapshot the tree first if the working tree may move; the record names the commit.
