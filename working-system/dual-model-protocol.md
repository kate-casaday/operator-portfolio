# Dual-model working protocol — Claude Code × Codex

*Public adaptation of the internal protocol, September 15, 2026 revision.  Owner: Kate.  Tooling, workspace, and review-log sections are removed; the rules are unchanged.*


## Purpose

Two frontier models, one repo, one owner.  Every substantive document gets an author and an independent reviewer from the other lab.  The red team is structural, not optional.  Files are the shared memory.

## Terms of engagement

1. **Files are the shared memory.**  Neither model sees the other's session, chat history, or memory.  Anything that matters is written to a file in this repo.  No handoff by memory, no "as we discussed."
2. **Both read the same operating system.**  `CLAUDE.md` (company context and file map), `AGENTS.md` (evidence protocol), and this file.  Codex reads `AGENTS.md` automatically; `AGENTS.md` points to `CLAUDE.md`.
3. **One editor per checkout at a time.**  Kate runs one model per terminal pane.  The model that is "on" edits.  The other waits.  Commit before every handoff so the diff is inspectable.  **Protocol and tooling files** (this file, `AGENTS.md`, `CLAUDE.md`, the review prompts, `ops/dialogue.sh`, `ops/review-cycle.py`) **are edited by one model at a time, and that model announces it first** — on Sept 15 both models rewrote them concurrently and one commit swept in the other's half-finished files.
4. **Equal roles, selected by Kate.**  Either model can author strategy, write code, analyze, review, reconcile, and synthesize.  No model gets a subject-matter monopoly or permanent final word.  For a joint cycle, Kate names the opener; it is Point, and the other is Red Team.  Existing document headers remain authoritative unless Kate starts a new cycle or reassigns them.  For single-model work, the model Kate engages is Point.
5. **The review lens is fixed.**  First review applies `strategy/red-team-feb16.md`: objections specific to Kate and this company, not generic VC objections; for each, *why it matters*, *what resolves it*, and the *investor version*.  Plus the `AGENTS.md` evidence protocol: every claim is traced to a transcript, a repo file, or labeled unverified.
6. **The reviewer does not rewrite.**  First review appends numbered findings to the review log and does not touch body text.  Second review (the author) reconciles each finding explicitly — accept, rebut, or defer — and makes the accepted edits.
7. **Disagreement escalates to Kate, not to a third model.**  Unresolved findings stay listed as "open — Kate decides."
8. **Nothing outward-facing ships with an open first review.**  Investor, partner, counsel, and press materials require status `final`.  Kate sends; models never send.
9. **Symmetric commit authority.**  Both models stage only their task files and commit before handoff.  Use `[author:model]`, `[review1:model]`, `[review2:model]`, or `[turn N:model]` as appropriate (`model` = `claude` or `codex`).  No model is forbidden to commit because of its identity.  If runtime permissions block Git, preserve the edits and report the exact block; never claim the commit happened.  A host runner can commit for either model, recording whose turn it was.  Joint cycles do not push automatically; publication is a separate action.
10. **Status ladder:** `draft → review1 → review2 → final → sent`.

## Joint review — September 15 revision

When Kate engages both models, use an alternating review cycle.  **Kate decided Sept 15: she names the total turn count in the command — `6` (three apiece, a quick pass) or `12` (six apiece, her usual).**  The number is always the TOTAL.  This supersedes the old default of six turns apiece and Claude-only synthesis for new joint cycles.  Existing dialogue records keep their original headers and history.

Kate specifies the document, Objective and opener.  If she names the opener in her request, record it without asking again.  If neither the request nor an existing header establishes it, ask which model moves first; never silently choose Claude.  Each turn begins by reading the complete document and the cited evidence needed for that turn.

| Turn | Model | Responsibility |
|---|---|---|
| 1 | Opener / Point | Draft the position or proposed revision against the Objective.  Name assumptions and open questions. |
| 2 | Other / Red Team | Objective check and evidence-grounded challenge; append numbered findings, without rewriting Point's draft. |
| 3 | Opener / Point | Reconcile every finding: accept, rebut with evidence, or defer.  Append the revised proposal. |
| 4 | Other / Red Team | Check the reconciliation and remaining risks, including new problems introduced by revision. |
| 5 | Opener / Point | Append the consolidated proposed result, disagreements and decisions needed from Kate. |
| 6 | Other / Red Team | Verify the consolidated result fairly represents the exchange.  Report resolved/unresolved findings and readiness.  Do not rewrite it or add an unreviewed substitute. |

For longer even cycles, alternate additional reconciliation/review pairs; the penultimate turn consolidates and the last verifies.  There is no extra synthesis turn after the cap.  No automatic extension or early consensus shortcut: complete the requested turns unless Kate stops the cycle or a tool/evidence blocker prevents meaningful progress.  A failed invocation is not a completed turn.

Within the cycle, all model contributions are append-only, including proposed revisions.  Earlier drafts and findings remain intact; mutable document headers may track progress in manual sessions.  The automated runner records progress in the committed turn headings and leaves the initial header unchanged.  Turn numbers describe model contributions, not messages to Kate or tool calls.  No cap on findings or review length.  Be concise where possible, but do not hide a material objection to fit 300 words.

**Objective disputes:** any model disputing what the document optimizes for reports that dispute verbatim as the first line to Kate and records `Open — Kate decides`.  The models may debate methods within the Objective, but neither may change it on Kate's behalf.  The last reviewer does not have decision authority over the opener.  Turn completion does not itself mean `final`; unresolved findings remain visible.  Kate sends outward-facing material, and it still requires a reconciled final artifact.

**After the cycle:** if the final check is clean, Point may apply the already-reviewed consolidated text to the deliverable with a provenance link to the cycle.  Any substantive new revision needs a new review, not a hidden seventh turn.  If an existing document has a Point, select that model as opener for a reconciliation cycle, or record Kate's reassignment explicitly.  A separate cycle file can review an existing artifact without altering its historical review log.

## Single-document review outside a joint cycle

The existing author → first review → reconciliation workflow remains available when Kate asks for one review, rather than engaging both in a cycle.  Determine roles from the document header, never the model's brand.  Reviewers append findings; Point applies accepted edits.  Prefixes and evidence rules above apply equally to both.  Existing status values such as `draft-complete ... awaiting ...` count as drafts awaiting first review.


## Required header (top of every new document)

```
> **Point:** <Kate-selected model> (author + reconcile) · **Red Team:** <other model> (review)
> **Objective:** <the one thing this document is optimizing for, in one sentence>
> **Status:** draft · **Last touched:** 2026-09-14 by Claude Code
```

## Required review log (bottom of every reviewed document)

```
## Review log

### First review — Codex — 2026-09-14
**Objective check:** agree with the stated objective / **dispute — route to Kate:** <why>
1. **[Finding]** — the problem.  Why it matters.  What resolves it.  *Severity:* blocking / material / minor.

### Second review — Claude Code — 2026-09-14
1. Finding 1 — accepted / rebutted / deferred — what changed, or why not.
Open — Kate decides: (list or "none")
```
