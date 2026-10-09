# Template: batch-close prompt

*Template.  Client name and every figure are illustrative.  The real ones are the client's.*

A batch-close prompt runs the last scoring pass of a phase.  Its job is to produce a small, named set of outputs with labels and to stop.  Every output has an expected signature where one can be predicted, so a result outside the range is flagged rather than explained.

---

# Analyst instance: Northfield Medical Group Phase 1 close, census and score ladder

**Mode:** read-only against the warehouse.  Authoring permitted in `v28/inputs/` and `v28/outputs/` on this machine only.
**One batch.  Hard stop at the end.  Do not continue into interpretation.**

## STEP 0: prove access

```sql
SELECT 1 AS connectivity_check;
```

Full connection string as given.  On failure: stop, verbatim error, wait.  No re-authentication, no server switching.

## Standing rules

- Show the exact SQL and the actual returned rows.  Errors verbatim.  Note any adapted table or column name.
- No summaries, no findings, no celebration, no dollar figures.
- Write results to `v28/outputs/close_<date>.md` incrementally, one task per section, line count confirmed after each append.
- Member-level extracts go only to `phi_out/`, which is gitignored.  Counts only in chat and in the results file.
- Where a signature is stated below, compare your result to it and **flag** a miss.  Do not fix a miss.

## Task 1: census, per book

Produce the scored population for each book, from the authoritative membership source named in the judgment-call log, not from any dashboard table.

| Book | Expected signature | Rule |
|---|---|---|
| MSSP ACO, attributed to the group | 10,000 to 11,000 distinct members | Exact match to the frozen census is required.  A delta of more than 0 is a flag |
| Provider-sponsored plan, attributed to the group | 4,500 to 6,000 | Attribution filter applied; the whole-plan count is not the census |
| Delegated payer one | 7,000 to 8,000 | Count only.  No score computed |
| Delegated payer two, including the delegated sub-roster | 6,000 to 9,000 | Count only.  Sub-roster flagged in a separate column |

Report: distinct members; members assigned a segment; members missing a segment; the identity that the two sum to the total.  Report the ESRD share.  Above 5% is a flagged finding, not a scoring blocker.

## Task 2: sample test before real data

Run the CMS model package on the publisher's shipped sample inputs, end to end.  Include the output in the results file.  If the package needed a compatibility patch, include the diff.  The patch may touch only the named utility file.  If it touched anything else, stop.

## Task 3: scenario inputs and scoring

Build inputs for scenarios S0 through S4 for the two books with claims in the warehouse.

- S0: demographic floor only.
- S1: S0 plus claimed conditions.
- S2: S1 plus the finalized true-gap pairs.
- S3: S2 plus open confirmed suspects.
- S4: S3 plus recapture headroom, deduplicated against S3.

Score every scenario.  Report mean score per scenario per book, with the member count scored.

**Signatures and gates:**
- Monotonicity: the mean strictly rises S0 → S1 → S2 → S3 → S4 within each book.  Any inversion is a flag.  Stop that book.
- Ceiling: S2 minus S1 lands at or below the coefficient sum of the true-gap pairs, roughly 20 on the illustrative list.  Above the ceiling is a double count.  Flag.
- Scenario files are hash-distinct.  Two scenario files with the same hash is a flag.

## Task 4: judgment-call log

Write `v28/outputs/judgment_calls_<date>.md` with one line per convention applied: segment source per book, the default used for members with no enrollment segment, the status vocabulary used to define "confirmed," and the mapping vintage.  A missing log makes the numbers unusable.

## Task 5: the two delegated payer books

For each, write a readiness paragraph, not a score.  State what the supplied file contains, what it lacks, and what would be needed to baseline the book.  Label the paragraph **Consultant-observed** or **Client-reported** as appropriate.

## Handback

Return, per task: the output file path, the figure and its label, the signature result (match or flag), and the gate result.  List blockers as only what a person must supply.  Commit and sync.  Stop.
