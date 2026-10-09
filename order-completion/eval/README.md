# Conversation evaluation

Two case sets, kept apart on purpose:

| File | Purpose | Rule |
|---|---|---|
| `dev_cases.json` | Development set.  Used to build and fix the rule-based mock and the engine. | Any case here is a **regression case**, not unseen evidence. |
| `heldout_cases.json` | Held-out set.  Not used while building or fixing in round two. | The moment a case is used to guide a fix, move it to `dev_cases.json`. |

Each case has synthetic patient text, an expected intent (closed vocabulary in `ocp/models.py`), optional
expected constraints, and tags (paraphrase, combined, caregiver, correction, adversarial, unexpected …).

## Rule-based simulation (no credentials, no cost)

```bash
python3 eval/run_eval.py --adapter mock --set dev
python3 eval/run_eval.py --adapter mock --set heldout
```

These report what the **deterministic mock classifier** does.  They are not model accuracy.  STOP / HELP /
wrong-number cases are decided by code before any model (`decided_by: rule`), exactly as in the engine.
Results land in `eval/results/<timestamp>-mock-<set>.json` and the dashboard's "Open product decisions and
evaluation failures" panel shows the latest runs.

## Live-model evaluation (PAID; not run in round two)

Prerequisites: `pip install anthropic`, a credential (`ANTHROPIC_API_KEY` or `ant auth login`), and Kate's
go-ahead on spend.  The command refuses to run without `--yes-i-accept-paid-calls`.

```bash
python3 eval/run_eval.py --adapter anthropic --model claude-opus-5 --set heldout \
    --budget-usd 1.00 --max-calls 20 --max-output-tokens 200 --yes-i-accept-paid-calls
```

**Limits enforced by the runner**

- `--max-calls` (hard on runner invocations): the runner never starts more classify() invocations than this.
  The live adapter is constructed with SDK transport retries **disabled** (`max_retries=0`) so one invocation is
  one provider request; with `--adapter routed`, one invocation may be up to two requests (cheap model, then
  the fallback), and the runner counts every recorded attempt.
- `--max-output-tokens` (hard): passed as the request's `max_tokens` on every adapter the factory builds (both
  models in `routed` mode); output cost per call is capped.
- `--budget-usd` (conservative guard, **not a guaranteed ceiling**): before each call the runner estimates that
  call's worst-case cost from the **actual serialized request** (the system prompt, the full user content the
  adapter builds — partner context, history, open orders, preferences, the text — and the JSON output schema,
  at characters/3, plus 200 characters of headroom, priced at the model's input rate; in `routed` mode the estimate is doubled to reserve the fallback request and `--max-calls` counts both; output at
  `--max-output-tokens` × output rate) and stops if actual spend so far plus that estimate would exceed the
  budget.  Actual input tokens are known only after the call, so a single call can exceed its estimate; the
  overshoot is bounded by the difference between the estimate and the real prompt, and the output side is
  capped.  Every attempt with known usage (successful or failed, including refusals and non-JSON output whose
  response carried usage) is added to spend at its own model's price and recorded; an attempt with no usage is recorded as
  `usage.known=false`, not as zero.

**Estimated cost of the default command (labeled assumptions)**

| Item | Value | Basis |
|---|---:|---|
| Cases in held-out set | 19 | file (one case moved to the development set after guiding a fix) |
| Rule-decided (no model call) | 2 | STOP / wrong-number cases |
| Model calls | ≤ 17 | `--max-calls 20` cap |
| Input tokens per call | ~1,200 | [A] the runner's own pre-call estimate for a short reply against the default context is ~1,180 tokens at chars/3 |
| Output tokens per call | ≤ 200 | `--max-output-tokens`; a JSON intent object is ~80 |
| Claude Opus 5 price | $5 / $25 per MTok | `costs/prices.json`, read 2026-09-15 |
| **Estimate at stated assumptions** | **≈ $0.20** | 17 × (1,200 × $5 + 200 × $25) / 1M — not an absolute worst case; the guard stops the run before the budget, and the results file reports actual spend |
| **Expected** | **≈ $0.13** | output ~80 tokens |

Running the 31-case dev set the same way is ≈ $0.33 at the same assumptions.  Sonnet 5 would be 2.5× cheaper; Haiku 4.5
5× cheaper.  These are estimates from the price file, not a quote; the results file reports the actual spend.

**What a live run does and does not establish.**  It measures intent + constraint agreement on 20 short
synthetic replies against expectations written by the same people who wrote the prompt.  It does not measure
real patient language, real conversation length, or downstream workflow correctness.  Treat it as the first
signal, then build a labeled set from partner-approved samples.


## Wording evaluation (version 3): does the writer say what the voice principles ask, within the facts?

```bash
python3.11 eval/run_compose_eval.py --composer fact                                            # deterministic writer, no cost
bin/run.sh eval-compose   # not a mode; use the venv directly:
.venv/bin/python eval/run_compose_eval.py --composer anthropic --model claude-opus-5 --budget-usd 1.00 --max-calls 40 --yes-i-accept-paid-calls
```

Cases live in `compose_cases.json`.  Each replays a synthetic thread through the real engine (mock classifier, so
the application's decision is deterministic) with the chosen composer writing the words, then checks: the fact
check passed (the composed text was sent, not the template fallback) plus the case's expectations (acknowledgement
near the start, relevant hours only, no repeated address/prep/link after a change, question count, segments, STOP
footer, forbidden words).  Every text is written to `results/<ts>-compose-<composer>.md` so the wording can be read,
not just scored.  The 20 openers on each fresh engine are written by the deterministic writer so only the case's own
turns are paid.

| Run (Sept 15, 2026) | Result | Spend | Notes |
|---|---|---:|---|
| deterministic fact writer | 10/10 | $0 | the structure the live model is shown as its example |
| Claude Opus 5, effort low, first prompt | 1/2 then budget stop | $0.67 | the model repeated address/prep after a change and echoed a patient's "6" the checker did not allow; the runner also paid for all 20 openers per case (fixed) |
| Claude Opus 5, second prompt (already-sent facts, opener structure, patient-echo allowance) | 7/10 | $0.17 | misses: promises turned into questions (plan confirmed, cost, correction) |
| Claude Opus 5, final prompt (`ACTION_GUIDE`: must-say and question-allowed per action) | **10/10** | $0.17 | 14 compose calls |
| Claude Sonnet 5, final prompt | **10/10** | $0.07 | 14 compose calls |

`eval/opener_samples.py` writes the first text for all 20 synthetic patients with the chosen writer: **20/20 on
Claude Opus 5 with zero fact-check fallbacks** ($0.29) after the structure was made explicit; the first attempt
before that ignored the visit date and included addresses.  Results: `results/20260915T170642-openers-anthropic.md`.

**Live classification (first run, Sept 15):** held-out **17/19 on Claude Opus 5** ($0.05); both misses classify a
message that names a site and a day as `willing` rather than `confirm_plan`.  These two cases now guide the next
prompt fix and therefore move to the development set per the rule above when that fix is made (not yet done).

**After the Codex closing review (same evening):** the fact check was rebuilt as an allowlist (see the review record, V3-1)
and the ten bypass texts became a regression.  Re-runs against the stricter checker: **Claude Opus 5 10/10** ($0.18),
**Claude Sonnet 5 10/10** ($0.07), **20/20 openers on Opus 5 with zero fallbacks** ($0.32).  cw-10 now asserts the timed
test's start-by limit is in the text.  Results: `results/20260915T173738-compose-anthropic.md`,
`results/20260915T173815-compose-anthropic.md`, `results/20260915T173926-openers-anthropic.md`.
