# Template: analytical validation framework prompt

*Template.  Client name and every figure are illustrative.  The real ones are the client's.*

This is the shape of the prompt that closes an analytical phase.  It is pasted into a fresh session of the analyst instance inside the client's perimeter.  It enumerates every claim the draft report makes that has not yet been validated against source, names the method and the gate for each, and names the evidence label each must resolve to.  It does not rewrite the report.  A later corrections pass consumes its handback.

---

# Analyst instance: Northfield Medical Group Phase 1 closeout, analytical validation

**Mode:** read-only against the warehouse.  Authoring permitted only in `validation/` on this machine.
**Companion:** the local repository is the system of record.  This prompt validates its claims.  It does not reopen its decisions.

**Purpose.**  Enumerate the analytical validation that must complete before the report is rewritten.  Each item names the exact claim it validates, the method, the QA gate, and the label it must resolve to on return.

**Evidence labels.  Use these exact words on every returned figure:**
Measured · Model-scored · Client-reported · Consultant-observed · Inferred · Directional · Pending validation · Unknown.

**Non-negotiable rules.**
- Never invent data.
- Never silently reconcile a contradiction.  Report both sides.
- Do not work off a derivative or summary file when the live warehouse can be queried.
- If a query cannot run, the item stays **Pending validation**.  It does not get a guessed number.
- No dollar figures.  Counts and condition-level detail only.
- Write results to `validation/handback_<date>.md` incrementally, one section per workstream, and confirm the line count after each append.

---

## STEP 0: prove access.  Mandatory.  Stop on failure.

```sql
SELECT 1 AS connectivity_check;
```

Run it with the full connection string exactly as given: `<tool> -S <server>,<port> -d <database> -G`.  State the environment: server, database, load state, and the maximum claim date for each book.  If this fails, stop and hand back the verbatim error.  Do not re-authenticate.  Do not try another server.  Do not proceed against any derivative file.

---

## Workstream A: census and score ladder

Already specified in `prompts/batch-close.md`.  Run that prompt.  Do not re-specify here.  On return, each figure carries a label:

- Continuing-panel scores per book → **Model-scored**.
- Full-year scores for a book whose roster is not yet in the warehouse → **Pending validation** until the roster is found; if absent, stays **Pending validation** or **Unknown**, never substituted with a wider population.
- The first delegated payer's supplied score → **Client-reported**.
- The second delegated payer's score → **Unknown**.  The payer supplies none.

## Workstream B: the missed-visit figure, lineage and refinement

**Validates:** the draft's claim that "about 9,000 flagged conditions had their patient in the room and were never worked."  The exact join logic behind that figure is **Consultant-observed, unverified**, and the client asked for a primary-care-visit filter.

1. Reproduce the number from source, documenting every join.  State the base tables, the definition of "linked to an appointment," the definition of "appointment occurred," and the definition of "never worked."  Emit the exact row count and confirm it reproduces the figure, or report the delta and why.
2. Apply the primary-care-visit filter: exclude no-shows, same-day cancellations, and non-primary-care visit types.  Report the refined count and the drop.
3. Three-bucket the linked universe: visit-linked, member has a future visit the queue cannot see, no future visit anywhere.
4. **Deliver:** the reproduced figure with full lineage → **Measured**; the filtered count → **Measured (primary-care basis)**; any assumption that remains → **Pending validation**.

## Workstream C: selection bias among reviewed rows

**Validates:** the draft's statement that "selection bias among reviewed rows is likely but not yet quantified."  Only about 4% of queue rows carry any coder disposition, and the not-present rate is computed on that subset only.

1. Characterize the reviewed subset against the full queue on observable dimensions: line of business, condition value distribution, suspect source, visit-linked share, practice concentration.  Where they differ materially, that difference is the bias.  Quantify it.
2. State plainly whether the subset's not-present rate can be generalized to the full queue and in which direction it skews.
3. **Deliver:** a characterization table → **Measured** for observed differences, **Inferred** or **Directional** for any generalization.  Do **not** produce a "true" full-queue reject rate.  That would be inventing data.

## Workstream D: true gap, coefficient sum to modeled score

**Validates:** the draft's correction that a raw coefficient sum of about 20 overstates the modeled lift, which should land near +0.004 average score on the continuing panel because the model nets hierarchies.

1. Take the finalized true-gap pair list at HCC grain.
2. Score the scenario with the gap added against the scenario without it, on the continuing panel, through the CMS model.  Report the average modeled delta.
3. Confirm which book the pairs sit in and re-list the top value-ranked conditions.
4. **Deliver:** coefficient sum → **Consultant-observed, Directional**; modeled delta → **Model-scored**.  The report cites the modeled figure as primary.

## Workstream E: suppression rate, reproduce and decompose

**Validates:** a suppression rate of about 70% that runs in the draft "without a stage attached."

1. Reproduce the rate from source.  State exactly what numerator, denominator, and confidence-band logic produces it.  If it cannot be reproduced from the warehouse because the logic lives upstream in the client's platform, say so and mark **Pending validation, routed to the client's technology lead**.  Do not guess the construction.
2. To the extent the data allows, decompose engineering-stage suppression from coder-stage suppression.
3. **Deliver:** the reproduced rate → **Measured** if reproducible, else **Pending validation** or **Unknown**; the two-stage split → **Measured** where computed, **Inferred** where not.

## Items explicitly not for this instance

These are resolved by people, not queries.  Track them.  Do not compute them.  Do not fabricate answers.

- The semantics of the platform's disposition statuses → the client's technology lead.
- Whether coder actions write back to the platform's condition layer → the client's technology lead.
- Contract economics: plan revenue basis, delegated terms, employed-physician count → the client's executives.
- An anomalous dialysis share in one book, real or coding artifact → the client's data lead.
- The MSSP cap formula → CMS methodology plus the client's prior-year score.

---

## STEP 6: handback

Return, per workstream: the figure, its **label**, the query lineage (tables and joins), and the QA gate result.  List **blockers** as only what a person must supply that this instance could not have queried itself.  Commit and sync outputs.  Validate before handback.  No unproven number travels.  Stop.
