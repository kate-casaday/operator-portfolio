# 04. Document red team and go / no-go

The analysis can be right and the report can still be unsafe.  A frozen baseline presented as current.  An absolute sentence where the evidence supports a bounded one.  A note to self that survived into the client copy.  A member-level row in an appendix.  This is the method that gets a draft from the reviewer instance's hands to a client working team without any of that.

## The sequence

1. **Claim inventory.**  Every quantitative claim in the draft, listed, with the artifact it came from.  A claim with no artifact is removed or relabeled.
2. **Arithmetic audit.**  Every sum, difference, and rate in the draft recomputed from the raw returns.  Not from the summary.  From the rows.
3. **Live and frozen basis separation.**  Analyses run at different times see different data.  The report must stamp every figure with its basis date, and a frozen-baseline figure may never be presented as the current value, nor a live value inserted into a frozen table without a stamp.
4. **Figure-disposition ledger.**  Every figure in the draft gets a row: source artifact, run date, status (survives, superseded, retired), and the role it may be quoted in.  This ledger becomes the authority for the disposition index described in `02`.
5. **Red-team prompt.**  A fresh session of the reviewer instance, with no drafting context, reads the draft against the ledger and the raw evidence and writes two files: a validation report and a list of blocking corrections.  Blocking corrections only.  No style notes.  No optional improvements.
6. **Corrections applied, with a resolution file** naming what changed and why.
7. **Go / no-go prompt.**  Another fresh session checks only that the named blocking corrections were resolved.  It does not reopen settled questions.  It ends with exactly one verdict.
8. **Client-safety scan**, inside both prompts.  No PHI.  No member-level rows.  No internal process language.  No commercial terms.  No prompt contents.  No drafting markers.  No false "final" label.  No claim that the next phase was elected.

Two things make this work.  The red-team session is cold.  It did not write the draft and it is not asked to improve it.  And the verdict vocabulary is closed.  There are three possible endings and the model must pick one.

## Template: final red-team review

*Template.  Client name and every figure are illustrative.*

```markdown
# Reviewer instance: Northfield Medical Group Phase 1, version 2.2, final red-team review

## Role

Conduct a focused independent review of the corrected version 2.2 package.

This is not a request to reopen every settled analysis.  Determine whether the
lifecycle correction was integrated accurately, whether the two analytical
timestamps remain separated, and whether the package is safe for working-team
circulation.

Do not edit the client files during the review.

## Client-facing inputs

- `Northfield_Phase1_V2.2_Working_Draft.md`
- `Northfield_Phase1_V2.2_Technical_Appendix.md`

## Evidence

- the lifecycle audit, its raw output, and its SQL
- the patch ledger for the draft
- the figure-disposition ledger

Do not treat the audit's conclusions or the patch ledger as authoritative when
they conflict with the raw output, the query window, or the frozen baseline.

# 1. Timestamp and basis separation

Confirm the report distinguishes:

## Live view, run date A
- 100,000 total queue rows
- 80,000 untouched
- 14,000 actioned pre-visit while open
- 6,000 verified post-visit

## Frozen baseline, run date B
- 98,000 total queue rows
- 94,000 status open
- the routing split table
- the frozen quarterly throughput figures

No frozen figure may be presented as the live value.  No live value may be
inserted into a frozen table without a stamp.  If the row-count difference
between the two could reflect a layer transformation rather than a refresh,
require an explicit limitation before circulation.

# 2. Arithmetic

Recompute every identity in the draft from the raw output, for example:
- 80,000 + 14,000 + 6,000 = 100,000
- 100,000 − 80,000 = 20,000

Confirm that "status open" is never defined as "untouched," that pre-visit
disposition and post-visit verification are separate constructs, and that no
total is formed by summing counts from different grains.

# 3. Absolute sentences

Reject any sentence that implies every visit-linked row was worked, every
documented condition was reviewed, or any population is complete, unless the
raw output supports the absolute.

# 4. Regression

Confirm the correction did not change any figure outside its scope: the
quadrant identity, the capture-rate denominators, the scenario values, the
cohort labels, and the evidence boundaries for the two delegated payer books.

# 5. Client-safety scan

Confirm: no PHI or member-level rows; no internal prompt content, commercial
terms, notes to self, or drafting markers; no unsupported "audit exposure"
conclusion; no claim that Phase 2 was elected; no false "final" label.

# Required outputs

Create:
1. `Northfield_Phase1_V2.2_Red_Team_Validation.md`
2. `Northfield_Phase1_V2.2_Blocking_Corrections.md`

The corrections file contains only errors that must be resolved before
circulation.  No stylistic preferences.  No optional additions.

End the validation report with exactly one verdict:
- `READY FOR CLIENT WORKING-TEAM REVIEW`
- `READY AFTER BLOCKING CORRECTIONS`
- `NOT READY: MATERIAL ANALYTICAL VALIDATION MISSING`

Also state: the number of blocking corrections; whether live and frozen bases
remain separated; whether every executive-summary figure has named lineage;
whether both client files are safe to attach; the exact client filenames; and
that all internal validation materials remain unshared.

Stop after producing the two files.  Do not edit version 2.2.
```

## Template: final go / no-go

*Template.  Client name and every figure are illustrative.*

```markdown
# Reviewer instance: Northfield Medical Group Phase 1, version 2.1, final go / no-go

## Role

Perform a narrow closure check of the red-team-corrected package.

This is not another red-team exercise.  Do not reopen settled questions, redraft
the report, or generate improvements.  Verify only that the previously
identified blocking corrections were resolved and that the package is safe to
circulate.

## Inputs

- `Northfield_Phase1_V2.1_Working_Draft.md`
- `Northfield_Phase1_V2.1_Technical_Appendix.md`
- `Northfield_Phase1_V2_Blocking_Corrections.md`
- `Northfield_Phase1_V2.1_Red_Team_Resolution.md`

Use the repository only to confirm the exact source references already named in
the corrected files.  Do not perform a new broad audit.

## Required checks

### B1: the payer-file score
Confirm the value is identified as a payer-file average, not a model score;
that it is not presented as a reconciled book RAF; and that its lineage names
the artifact.

### B2: the capacity table
Confirm it is either correctly restored or accurately omitted; if restored,
labeled directional, with exact lineage and the coverage caveat disclosed.

### B3: the intermediate-count conflict
Confirm neither client file asserts an unresolved intermediate count, and that
a reconciliation control is recommended before production use.

### B4: internal language
Confirm no note-to-self language, no "red team" language, no process jargon,
no commercial discussion, no prompt contents, no internal change-log material.

### B5: named lineage
Confirm the appendix names the exact artifact and run date for every crosstab
in the executive summary.

## Client-safety scan

No PHI or member-level rows.  No slot or handback markers.  No false "final"
label.  No unsupported scores for the delegated payer books.  No retired
figure.  No unqualified "audit exposure" conclusion.  No statement that Phase 2
has been elected.

## Output

Create `Northfield_Phase1_V2.1_Final_Go_NoGo.md`:

    # Final Go / No-Go
    ## Verdict
    GO FOR WORKING-TEAM REVIEW

or

    # Final Go / No-Go
    ## Verdict
    NO-GO: BLOCKING CORRECTION REMAINS

Then a table: check, pass or fail, evidence location, required correction if
failed.  End with the number of unresolved blockers, whether both files are
safe to attach, the exact filenames, and confirmation that the internal change
log and red-team files must not be attached.

Do not edit the version 2.1 files.  Stop after writing the one output file.
```

## What this produced on the real engagement

Three versions of the report in four days.  A retired-figures list in the final appendix so that nobody, including the client's own analysts, resurrects a number that died.  And a final that the client's analytics lead restated in his own words in the closing session, which is the only test that matters.
