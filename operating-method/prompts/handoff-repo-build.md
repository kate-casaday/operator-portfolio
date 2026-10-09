# Template: analyst handoff repository build

*Template.  Client name and every figure are illustrative.  The real ones are the client's.*

At the close of an engagement the client's own analysts need to be able to rerun everything without me.  The working repository on the analyst instance is an archaeology site: dated prompts, superseded outputs, files with known errors, and a git history that once held protected extracts.  The handoff repository is the opposite.  One version of everything.  Current.  Validated.  Documented.  Built by inclusion, never by filtering the working tree.

The prompt has two parts.  The build.  Then, in a fresh session, an adversarial audit written as if the client's most exacting analyst were reviewing it.

---

# Analyst instance: build the handoff repository for Northfield Medical Group

**Cardinal rule.  The working repository is never pushed to any remote.  Not its tree, not its history.**  Its history contains extracts that were later deleted.  A push would carry them.  The handoff repository is a fresh repository, populated by explicitly copying allowlisted files in.

## Structure

```
handoff/
  README.md          the instruction manual
  MANIFEST.md        every artifact: version, date, supersedes, validation status, report section it feeds
  sql/               the reconciliation queries, final versions only
  v28/               the complete scoring chain
  reconciliation/    the four-quadrant job as a repeatable refresh-cycle procedure
  docs/              final report and technical appendix, plus the runbook
```

## Inclusion rules

1. **One version per artifact.**  For each query, include only the final validated version.  If two candidates could be the final, stop and list the ambiguity in the handback rather than guessing.
2. **The report's lineage index is the completeness checklist.**  Every artifact it names must exist here, or be listed in the handback as intentionally excluded with a reason.  Every figure family in the final report must be reproducible from here.
3. **No PHI.**  No member-level files.  No identifiers, names, dates of birth, member IDs, extracts, or worklists.  Aggregate outputs are fine.  Before commit, sweep staged files for identifier-shaped strings, date-of-birth-shaped dates in data rows, and known member-ID column headers.  Report the sweep result.
4. **Known-bad files excluded by name.**  List them.  If their evidence belongs here, use the raw SQL and raw output only.
5. **Superseded is absent.**  No dated prompt files.  No drafts.  No earlier versions beside a final.  No batch output that was later corrected.  The working repository's git history is the archive.  This one starts clean.

## The scoring chain directory

- The CMS model package plus the compatibility patch, with a short note: what changed, why, and the sample-input run that proves it does not alter model logic.
- The scenario driver, **made portable.**  It currently carries absolute paths.  Parameterize to a config block or paths relative to the repository root, then prove it by running one small scenario from a fresh clone path.  Do not hand back an unproven driver.
- The input builders for every scenario and cohort.  This is the code the client's analysts said they never received.  The code, not the runbook.
- The QA gates document: sample-test evidence, population integrity, monotonicity, ceiling, hash distinctness.
- A placeholder for the parallel runner, built live with the client's analysts in the co-run session.

## SQL header blocks

Each query opens with: purpose, grain, source tables, join keys, known caveats, last-validated date, which report section it feeds, and validation provenance naming the client's field-by-field review and the method decisions it incorporates.

## MANIFEST format

| Artifact | Version and date | Supersedes | Validation status | Produces (report section) |

One row per file.  "Supersedes" names what died so nobody resurrects it.  The standing rule, stated at the top: nothing enters without a date and a supersedes line, and old versions never coexist with new.

## README

Audience: an analyst who has never seen this work.  Sections: what this repository is and the one-version rule; environment prerequisites, including the known two-install failure mode and how to avoid it; how to run each query; how to run the scoring chain end to end with expected runtime; the refresh-cycle reconciliation procedure; where each headline finding comes from, in at most two hops.

## Handback

Write `HANDBACK_BUILD.md` and commit: the full tree; the manifest as built; the PHI sweep result; the driver portability proof with command and output; a lineage-index coverage table; and every ambiguity or gap you could not resolve.  List.  Do not guess.

Do not delete anything from the working repository.

---

# Part 2: adversarial audit.  Fresh session.  Cold read.

Role: you are the reviewer for the client's most exacting analyst.  Their standard, legible from their own artifacts: explicit grain and denominator on every output, validated join keys, environment provenance on every query, and nothing an agent could misread.

1. **Contradiction grep.**  Search every file for the known-bad join pattern and any join that contradicts the validated corrections.  One inconsistent file poisons agent-assisted work.  Zero tolerance.
2. **Banned-strings gate.**  Build the list from the report's own retired-figures section.  Grep for each.  Zero hits outside the report and appendix, which retire them explicitly.
3. **Environment provenance.**  Every query states which environment it ran against and when.
4. **Cold-analyst test.**  Following the README alone, can you locate the artifact behind each headline finding in two hops or fewer?  Can you state how to rerun the scoring chain without opening any file outside this repository?  Where no, fix the README.
5. **Grain and denominator check.**  Every query and every aggregate output states both.  Flag any figure whose denominator requires tribal knowledge.
6. **PHI re-sweep**, independent of Part 1's.
7. **Simplicity pass.**  Anything a cold analyst does not need to run or understand the work: flag for removal.  This repository errs small.

Write findings to `AUDIT_REPORT.md`: issue, file, severity, fix applied or recommended.  Fix what is mechanical.  Commit.  Nothing is pushed until the operator has read the audit report.

---

## Operator note, not part of the prompt

The prompt library and the orchestration method stay out of the handoff repository.  Deliverables are the client's.  Models, prompts, and methods are mine under the agreement.  The operational runbook goes in.  The meta-method does not.
