# 05. Per-program value method

How a risk score becomes dollars is the place where this field makes its most common mistake.  This is the frame I used, written as method.  No figures from the engagement appear.

## The terminal metric

RAF lift is the mechanism, not the result.  For an employed physician group the number a chief executive can act on is the employed-physician subsidy: what the group spends above what the physicians bring in.  Captured risk revenue shrinks the subsidy.  So the closing slide is "this shrinks the subsidy by X per physician per year," and the RAF build-up is the slide before it.  Pick the terminal metric before any math.

## The yardstick

Score on the client's own convention first.  If their analysts apply one coefficient set to every member, apply the same, so the numbers reconcile with theirs.  Then show the segment-accurate version as a refinement lane with its own sizing.  Presenting on a different yardstick than the client's is how a correct number loses the room.

## Each line of business pays differently

| Program | How a point of RAF becomes money | Caveat |
|---|---|---|
| **MSSP ACO** | Score moves the benchmark, which moves shared savings.  Use CMS-supplied scores as the floor.  A self-computed score never ties to MSSP economics because of renormalization and coding-intensity adjustment.  Aggregate score growth is capped. | Directional until per-member benchmark dollars are anchored |
| **Provider-sponsored health plan** | Cleanest lane.  The group is the payer.  Score times the per-member base rate, with the year's normalization applied.  Self-submission makes captured codes bankable in-year. | Needs the plan's actual base rate.  Weakens if the group exits the plan |
| **Delegated Medicare Advantage** | Dollars flow through the capitation or percent-of-premium terms.  Score times premium times the group's share. | Contract terms needed from the client.  De-delegation by tax ID changes who books the value |
| **Commercial** | Different model, different year, out of scope for a risk-adjustment phase | Say so |

A blended per-member rate applied across programs is the first error in this space.  It values an ACO member like a health-plan member, and both like a delegated member.  The programs differ by an order of magnitude in what a captured condition is worth and in when the money arrives.

## The layered build-up

One stacked bar per program, built from the floor up:

1. **Demographic floor.**  Age and sex factors only.
2. **Claimed conditions.**  What is on claims today.  This layer is current RAF.  It should reproduce the supplied score within tolerance.  If it does not, say why before showing anyone.
3. **True gap.**  Documented in the coding platform, absent from current claims, after lag is removed.  Work already done, revenue not banked.  The hardest layer to argue with.
4. **Lag recovery.**  Documented, claims window not yet caught up.  Mostly self-resolving.  Shown so the room sees it is not being claimed as intervention value.
5. **Open confirmed suspects.**  Coder-validated, awaiting a visit.  The workflow-improvement lane.
6. **Recapture headroom.**  Prior-year conditions not yet recaptured this year, deduplicated against layer 5 so suspect signal is not counted twice.

Value at stake is layers 3, 5, and 6, priced by the program's mechanism.  Layer 3 alone is the floor.  Layers 3, 5, and 6 together are the ceiling.  Audit-defense exposure, the claimed-and-not-documented quadrant, is reported beside the build-up as a risk number.  It is never netted.

## Floor and ceiling, stated as commitments

Present the floor as committed where the mechanism is clean.  Present the ceiling as sized but unpriced where contract terms are missing.  Under-promise on any book where the supplied score is capped, suppressed, or absent.  A number the client cannot reproduce from their own terms is a number they will not use.

## What goes where

The analyst instance supplies member-by-condition lists per layer.  Coefficients, normalization, and dollars are applied on my side.  This is not only a privacy rule.  It is the rule that keeps a model from multiplying a bug by a rate and calling it an opportunity.
