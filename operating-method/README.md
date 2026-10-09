# Operating method: Claude Code as a supervised analyst inside a client's data perimeter

This exhibit is the reusable method behind a real engagement.  In spring and summer 2026 I ran a 60-hour fixed-scope risk-adjustment assessment for a large employed physician group.  The group carries Medicare Advantage risk under delegated contracts with two payers, runs an MSSP ACO, and owns a provider-sponsored health plan.  The question was whether their coding workflow captured the conditions their physicians documented, and what the gap was worth.

The analysis ran on a Claude Code instance inside the client's own environment.  I never wrote the SQL.  I defined the questions, built the operating framework, ran the instance, caught what it got wrong, and decided what was true.  The client's analytics team independently validated the results.

## What this is not

- No client data.  No member counts, row counts, scores, or dollar figures from the engagement appear here.  Where a prompt needs a number, the number is invented and the prompt says so.
- No deliverables.  The report, the appendix, and the handoff repository belong to the client.
- No runnable SQL or model code.  The method is the point.
- No client identity.  Under the services agreement the client's identity and information are theirs.  Under §8 of the same agreement the prompts, methods, and analytical frameworks are mine.  This exhibit stays on my side of that line on purpose.

## The attribution, stated plainly

Kate defined the questions and the operating framework and was the operator inside the environment.  Claude executed the SQL and the CMS-HCC model code.  The client's analytics team independently validated the analysis.  Each half of that is real and neither half is the other.

## The shape in one picture

```
   CLIENT PERIMETER                                   MY SIDE (no PHI, ever)
   ┌──────────────────────────────────┐                ┌──────────────────────────────────┐
   │  Analyst instance  ("the analyst")│                │  QA instance  ("the reviewer")    │
   │  Claude Code on a cloud          │                │  Claude Code on my laptop         │
   │  workstation the client provisioned│                │                                  │
   │                                  │                │  Writes every prompt as a file    │
   │  Has: warehouse access, the CMS  │   prompt file  │  Reads only what the analyst      │
   │  model package, a working repo   │ ◄───────────── │  returns                          │
   │                                  │                │  Does: QA, synthesis, drafting,   │
   │  Does: runs SQL, runs the model, │   raw output   │  all dollar math, the client      │
   │  writes raw results to files     │ ─────────────► │  report                           │
   │                                  │                │                                  │
   │  May not: conclude, celebrate,   │                │  May not: touch the data          │
   │  compute dollars, leave the box  │                │                                  │
   └──────────────┬───────────────────┘                └───────────────┬──────────────────┘
                  │                                                    │
                  └──────────────────  Kate, the operator  ────────────┘
                     the only bridge between the two: paste, screenshot, judgment
```

One batch in flight at a time.  Prompts are files, never inline only.  Raw returns are never edited.  Dollars are computed on my side, and last.

## Files

| File | What it holds |
|---|---|
| `01-engagement-shape.md` | The two-instance setup, what each may see, and the standing rules every prompt carries |
| `02-evidence-labels-and-quote-safety.md` | The eight evidence labels, the disposition index, and the rule that a figure is quotable only through the ledger row that names it |
| `03-qa-gates-and-the-headline-that-was-an-artifact.md` | The day the first batch came back with a headline dollar range that was an artifact, how QA proved it, and the gate list that came out of it |
| `04-document-red-team-and-go-no-go.md` | How a client report gets from draft to safe to circulate, with the two prompts as templates |
| `05-per-program-value-method.md` | How a RAF point becomes dollars, by line of business, and why a blended rate is the first mistake in this field |
| `prompts/validation-framework.md` | Template: the evidence-label validation prompt |
| `prompts/batch-close.md` | Template: a batch-close prompt with named outputs, labels, and stop conditions |
| `prompts/handoff-repo-build.md` | Template: the spec for the analyst handoff repository |

All prompts use a fictional client, Northfield Medical Group, and illustrative figures.
