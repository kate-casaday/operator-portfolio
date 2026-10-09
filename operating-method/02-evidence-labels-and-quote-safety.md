# 02. Evidence labels and quote safety

A number in a client report has to carry its own provenance.  The reader should be able to tell, without asking, whether a figure was measured from source, produced by a model, told to us by the client, or guessed.  So every figure the analyst returns, and every figure the report prints, carries one of eight labels.

## The eight labels

| Label | Meaning | Illustrative example |
|---|---|---|
| **Measured** | Computed directly from source tables with named lineage.  Reproducible by rerunning the query. | "4.1% of queue rows carry any coder disposition."  Table, filter, and run date named. |
| **Model-scored** | Produced by running the CMS-HCC model over a defined population.  Reproducible from the input builder and the model package. | "Mean RAF on the continuing panel, scenario S1, is 0.87." |
| **Client-reported** | Stated by the client's staff or present in a client-supplied file we did not derive.  Not reproduced by us. | "The payer file's average score for that book is about 0.95." |
| **Consultant-observed** | Noted during the engagement from a walkthrough, a screen, or a conversation.  Not queried. | "Suspects are pre-filtered at a confidence threshold before they reach the coder queue." |
| **Inferred** | Derived by reasoning from Measured or Model-scored figures, with the inference stated. | "The reviewed subset is three times more likely to be visit-linked than the full queue, so the reject rate does not generalize." |
| **Directional** | Indicates magnitude or ordering only.  Not to be quoted as a point value. | "The MSSP book has the lowest pre-visit capacity of the four." |
| **Pending validation** | A query was specified but could not run, or a definition is still with the client.  Holds a slot.  Never holds a guessed number. | "Full-year MSSP continuing-panel score: pending the 2026 employed roster." |
| **Unknown** | Nobody has this.  Said plainly. | "The second delegated payer supplies no score.  Baseline RAF for that book is unknown in this phase." |

Three rules sit under the table.

1. **A figure that cannot run stays Pending validation.**  It does not get an estimate.  Inventing a number to fill a slot is the one unforgivable act.
2. **Never silently reconcile a contradiction.**  If two sources disagree, both sides are reported with their labels.  The reconciliation is a decision I make in the open, not a quiet choice the model makes.
3. **Never work off a derivative when the source can be queried.**  A summary file is not evidence.  The analyst once substituted repository notes for live queries.  The step-zero rule and this rule exist together.

## The disposition index

The analyst returned dozens of files over the engagement.  Most were superseded by a later batch.  Some carried figures that were later retired.  All of them stayed on disk, because raw returns are never edited.  Errors and dead figures stay in them as evidence of what was run.

That creates a hazard.  A dead figure in an old file looks exactly like a live one.  So the output folder carries an index that gives every file one of three statuses:

- **Fed final.**  Figures or findings survive into the final report, or the file is a process artifact consistent with the final record.
- **Superseded.**  Replaced by a later batch, failed a gate, or omitted from client-facing findings.  The successor is named.
- **Contains retired figures.  Do not quote.**  Carries one or more figures on the retired list.  The specific dead figures are named in the notes.

Some files are both.  A file can carry a retired framing and also be the named source for a figure that survived.  For those, the surviving figure is quotable only through the ledger row that names it.  Everything else in the file is off limits.

## The quote-safety rule

Before any number goes into a draft, an email, or a slide: find its row in the disposition index, then verify it against the figure-disposition ledger in the final report.  Where the index and the ledger disagree, the ledger wins.

A figure is quotable only via the ledger row that names it, in the role the ledger states for it.  A count that the ledger calls directional is not quoted as a point value.  A count that the ledger calls a frozen baseline is not presented as a current value.

## Why this much ceremony

Because the alternative is a report where a number that was wrong on day three survives to the client's board on day sixty because it looked right and nobody could trace it.  The labels, the index, and the ledger are the trace.  They cost about ten minutes per batch.  They saved the engagement at least twice.
