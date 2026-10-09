# 01. Engagement shape

## The problem the shape solves

The data I needed lived in the client's warehouse.  It is protected health information.  It cannot leave.  The analysis I needed to do was too large to do by hand in 60 hours and too consequential to trust to a model running unsupervised.  So the engagement ran on two Claude Code instances with a person between them.

## The two instances

**The analyst instance** ran on a cloud workstation the client provisioned inside their perimeter.  It had a command-line connection to the warehouse, the CMS-HCC V28 model package, and a working git repository on that machine.  It ran Claude Sonnet 4.5 for most of the engagement and Claude Opus 4.7 for the close.  Its job was to execute.  It ran the SQL I specified, ran the scoring model, and wrote what came back to files.

**The reviewer instance** ran on my laptop with no warehouse access and no protected data.  It ran Claude Opus.  Its job was everything the analyst instance was not allowed to do.  It wrote the prompts, checked the returns against expected signatures, kept the evidence log, drafted the client report, and did every piece of dollar math.

**I was the operator.**  I was the only bridge.  A prompt moved from my side to the analyst side by paste.  A result moved back by paste or screenshot.  Every judgment call about what a result meant, what to ask next, and what the client would hear was mine.

The reason for the split is not only privacy.  It is that execution and interpretation are different jobs, and the model that ran the query is the wrong model to tell you what the query means.

## What each side may and may not see

| | Analyst instance (inside) | Reviewer instance (my side) |
|---|---|---|
| Warehouse | Live, read-only unless a prompt says otherwise | Never |
| Member-level rows | Yes, in the box.  Extracts go to a gitignored folder and never into chat | Never.  Counts only |
| The client's deliverable | Never drafts it | Drafts it, from the evidence log |
| Dollar figures | Banned from its output entirely | Computed here, last, from counts the analyst returned |
| Conclusions | Executes.  Does not conclude | Interprets every return.  Reframes every "finding" |
| Commercial terms, relationship notes | Never | Yes, in a separate folder from the evidence |

## The loop

1. The reviewer instance drafts a batch prompt as a file in the method repository.
2. I read it, edit it, and paste it into the analyst instance.
3. The analyst instance runs the batch and writes the raw results to a dated file on its own machine.
4. I relay the results back.
5. The reviewer instance checks the return against the expected signatures, logs the evidence, updates the orchestration log, and drafts the next prompt.

One batch in flight at a time.  Nothing is interpreted in the same session that ran it.

## Standing rules, carried in every prompt to the analyst

These were earned.  Each one exists because its absence cost a day.

1. **Step zero is a trivial live read, with the full connection string spelled out.**  If it fails, stop, print the error verbatim, and wait.  Do not re-authenticate.  Do not switch servers.  Do not self-heal.  The analyst once guessed a legacy server from a bare database name, failed to log in, and launched a device-code login flow on its own.  That session was killed and the rule was written.
2. **Declare read-only or authoring at the top.**  A prompt that writes SQL to the repo says so.  A prompt that only queries says so.
3. **Evidence rules.**  Show the exact SQL and the actual returned rows.  Errors verbatim.  Note any table or column name you adapted.  No summaries, no findings, no celebration, no dollar figures.
4. **Stop and wait.**  Hard stop at the end of the batch.  Never continue into interpretation.
5. **Write results to a file, in full, incrementally.**  Terminal scrollback is not evidence.  One section per step, appended, with a line count confirmed after each.  The analyst once ran a whole batch cleanly and lost it in collapsed scrollback.  It then stalled twice trying to write a fourteen-section file in one response.
6. **Expected signatures.**  Where I can predict a range, the prompt states it.  That turns QA from judgment into arithmetic, and gives the analyst a tripwire to flag rather than a problem to "fix."
7. **Prompts are files.**  Every prompt sent to the analyst is a dated file in the method repository before it is pasted.  Nothing is inline only.
8. **Raw returns are never edited.**  A result file is ground truth for what was run, errors included.  Corrections are new files with a supersedes line.

## Failure modes the rules are designed against

- **Premature synthesis.**  The analyst ran five queries and announced a headline dollar opportunity built on a result that was a bug.  Dollars were banned from its output that afternoon.
- **Over-claiming.**  A partial overlap rate was labeled a critical finding before anyone had separated claims lag from crosswalk loss from genuine gap.
- **Silent adaptation.**  A model that hits an error may quietly rewrite the query.  Hence errors verbatim.
- **Claim-versus-reality drift.**  A results file said a score was self-calculated from claims when no score had been computed anywhere.  Never trust the model's description of what its code did.  Read the SQL.
- **Output evaporation.**  Results in scrollback, not files.
- **Mid-stream stalls.**  Giant single-response writes fail.  Chunk and checkpoint.

The counterpoint is also in the log.  When boxed by the rules, the analyst executed cleanly and its recap matched the evidence exactly.  The box works.

## What the client's team got

At the close, a separate handoff repository was specified for the client's own analysts.  One version of every artifact, a manifest with a supersedes line on each, a PHI sweep before commit, and a runbook a cold analyst could follow.  The prompt library and the orchestration method stayed out of it.  Deliverables are the client's.  The method is mine.  See `prompts/handoff-repo-build.md`.
