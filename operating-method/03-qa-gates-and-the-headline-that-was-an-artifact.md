# 03. QA gates, and the headline that was an artifact

## What happened

The core of the engagement was a four-quadrant reconciliation.  Take every condition a physician documented in the coding-opportunity platform and every condition that reached a claim.  Cross them.  Quadrant one is documented and claimed.  Quadrant two is documented and not claimed.  Quadrant three is claimed and not documented.  The second quadrant is a follow-up question after the join and claims lag are checked.  The third is a reconciliation question, not an established audit finding.  This grid is the measurement chassis.  Getting it right was the deliverable.

The first morning the analyst instance ran all five queries, it came back with "all five complete" and a headline: a headline opportunity range, with a bar chart.

By that afternoon the headline was gone.  Here is how.

## The QA that caught it

**The logical impossibility test.**  The reconciliation was run at two grains.  At the diagnosis-code level, a handful of members landed in quadrant one.  At the HCC level, zero did.  That ordering cannot happen in a correct build.  Every diagnosis code that matches rolls up to an HCC in the crosswalk, so any code-level match must also be an HCC-level match.  HCC-level matches below code-level matches means one thing: the two sides of the join carried different HCC strings.  A diagnostic batch that evening proved it.  One side wrote the HCC with a text prefix and the other wrote the bare number.  Exact string equality never matched.  Every documented condition fell into quadrant two, and quadrant two is what got multiplied into dollars.

**The join-grain error.**  The query also required the documented visit and the claim's service date to fall within one day of each other, as a hard condition of the join.  Risk-adjustment capture is a member-by-condition-by-year question.  A condition documented in January and supported by a claim in March is captured.  The one-day window threw it into the gap.  When the window was quantified it had discarded almost every true pair.

**The lag confound.**  The claims feed was current to April.  The documentation feed was current to June.  Anything documented after April had no claim yet, by timing, not by failure.  Quadrant two had to be split into genuine gap and pending lag before it meant anything.

**Month-inflated sums.**  The population query summed flag columns over member-months and reported them as member counts.  Distinct member counts were right.  Every "count of members with flag X" was roughly twelve times too large.

**Claim-versus-reality drift.**  The results file said the baseline score was self-calculated from claims.  No query computed a score anywhere.  The query assigned demographic segments and stopped.

None of this required domain genius.  It required reading the SQL instead of the summary, and knowing that a number which arrives before its chassis is validated is not a number.

## The re-anchor

My own deck from two weeks earlier said, in the speaker notes: do not present anchored numbers today, framework only, the math is mechanical once the cuts are trustworthy.  The model's premature headline violated my own operating discipline.  The fix was not only technical.  It was to re-anchor the engagement on the chassis.  The number was always supposed to come last.

## What changed in the method

- **Grain stated in every prompt.**  Member, member-condition, member-condition-year, or member-month.  Named before the query is written.
- **Vocabularies unified before any match.**  Both sides of a reconciliation derive their HCC from the same crosswalk, in the same format, before the join.
- **Dollars only on my side, only last.**  The analyst instance returns counts and condition-level detail.  It never multiplies anything by a rate.
- **Expected signatures in the prompt.**  Where a range can be predicted, it is written down, so a return outside it is flagged, not explained away.
- **A gate list** that a scoring output has to pass before any number enters a draft or a deck.

## The gate list, generalized

1. **Sample test on shipped reference inputs first.**  Before the model runs on real data, it runs on the sample inputs the model's publisher ships, end to end, and the output is in the handback.  If the model code needed a patch, the diff is in the handback and the patch touches nothing else.
2. **Population integrity.**  The scored population count ties to the census.  Members assigned a segment plus members missing a segment equals the total, and the split is reported.
3. **Source of truth stated for every segment.**  Which table, which column, which remap.  Not the convenient table.  The authoritative one.
4. **Monotonicity.**  Across scenarios that add conditions, the mean score strictly rises.  An inversion means a scenario file was built wrong.  Reject.
5. **Ceiling checks.**  The scored lift from adding a set of conditions lands at or below the sum of their coefficients.  Hierarchies net down, never up.  A lift above the ceiling is a double count.  Reject.
6. **Judgment-call log present.**  Every place a human chose a convention (which flag source, how blanks were defaulted, which status vocabulary) is logged.  A missing log makes the numbers unusable in front of the client's analysts.
7. **Readiness paragraphs instead of scores where data is absent.**  For a book with no score in the supplied files, the report says so and describes what would be needed.  It does not back into a number.
8. **On pass, and only on pass,** the scenario table moves into the deck build, and the per-program dollar math begins.

## What the gates cost

About thirty minutes per scoring run.  Over the engagement they rejected two scenario files, caught one double count, and surfaced a data refresh that had silently removed a large share of one book between two runs.  That last one would have become a finding about members gaining codes.  It was members leaving the universe.
