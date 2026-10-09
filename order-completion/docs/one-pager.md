# What the order-completion prototype shows — one page

*StealthCo · September 15, 2026 · v0.3 (round two) · synthetic data only · built by Claude Code, independently reviewed by Codex (internal review record, not included in this portfolio)*

**The patient problem.**  A health system writes lab orders that sit open for weeks: the patient meant to
go, couldn't find the time or the place, wasn't sure it was still needed, or already did it somewhere else and
nobody knows.  Every open order is care not delivered and revenue not collected.

**The idea in one sentence.**  When an order has been open past a threshold the partner sets, we text the
patient, remove the practical barrier (where, when, how), route anything clinical to the partner's own
clinicians, and let the partner's own lab result close the loop.

**The operating constraint.**  "Kate and an algorithm": automate the reliable part, hand the rest to a named
human, and measure how many human minutes each patient actually costs.

## Implemented and tested (runs on a laptop, no credentials; every line maps to tests in `tests/`)

- Import synthetic orders, apply a configurable 15–45-day overdue rule (an order awaiting a future intended
  date is never overdue), exclude cancelled and completed orders, contact only patients with partner consent.
- Handle the conversations we set out to handle: a caregiver with limited time who needs nearby options; a
  working parent who needs evenings or weekends; a patient who combines constraints, changes plans, or corrects
  us; a patient who says the labs are already done (and where); a patient who asks for a nurse or a real person;
  a patient who asks for fewer reminders or never answers.
- Remember what the patient stated — and only that — with its source and any correction, so later replies use
  it.  No profiling, no third-party data.
- Offer only verified locations and links; confirm plans only when the site is open that day within the
  patient's stated hours; say plainly that a plan is a walk-in plan, not a booking.
- Keep three kinds of "done" apart: partner-verified (the only automatic completion), patient-reported
  (with in/out-of-network flag), and human-attested completion outside the network.
- Route clinical questions and requests for clinical staff to the partner's clinician queue (even while an
  operational item is open) and operational questions to Kate's queue, with owners, deadlines and overdue
  flags.  Honor STOP everywhere, including during crashes and phone changes.
- **Kate's work surface:** play a synthetic patient through the same code path as a real text, see why each
  message was sent, flag a response, write what should have happened, and export a development task that
  carries the captured evidence separately from her interpretation.

## Simulated (labeled on every screen)

The "model" is a rule-based stand-in; texts never leave the process; the clinician handoff is a queue item
in our database — no one is notified by it, and clicking "accept" notifies no one either; it records that a
person has taken the item.  A real handoff needs the partner's own delivery channel.

## Prepared but not exercised

A live Claude adapter and a bounded, spend-guarded evaluation command (≈ $0.19 to run once); a Twilio adapter
with sending disabled.

## Partner-dependent

The data feed, the clinician queue's real delivery, the threshold, template approval, identity policy,
hosting under a BAA.

## Still a hypothesis

- That a language model reads real patient texts reliably.  On 19 held-out synthetic replies the rule-based
  stand-in agreed with the expected reading 15 times; that is a simulation result, not model accuracy, and it
  says nothing about what happens downstream.  No live model has been run.
- That patients reply, and at what rate; how many minutes a human spends per patient.  The synthetic fixtures
  were chosen to exercise failure paths, so they say nothing about real engagement or lift.
- That the economics hold.  Under labeled assumptions, software and text-message cost per patient-month is
  small at every scale; the cost that scales is human time: in a medium-engagement scenario, Kate's queue at
  10,000 patients is ~260 hours a month.  Which escalation rate fits which hours budget is tabulated in
  `costs/workload_results.md`; clinical escalation is a requirement, not a cost to drive to zero.

## Next evidence needed

One partner's real export for one month of stale orders, 200 real patient replies to label, and a named
clinician who agrees to own the queue.  That converts the three biggest hypotheses into measurements.

*Not investment material.  Draft; nothing here has been validated with a partner or a live model.*
