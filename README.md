# Kate Casaday: healthcare operations with Claude Code

I am a healthcare operator.  I ran risk-adjustment and value-based-care operations inside a large medical group, built the revenue system for a healthcare services company, and founded my consulting practice in 2023.  The practice has been full time since October 2025.  Since early 2026 I have done that work, and co-founded a healthcare company, with Claude Code as the working tool and OpenAI's Codex as the second reviewer.  This repository is the evidence.

Two exhibits and a description of the working system.  Neither exhibit was made for this page.  The method came from paid client work.  I built the prototype for my own venture in September 2026.  No client, patient, or partner data appears anywhere.

| Section | What it shows | Read first |
|---|---|---|
| [`operating-method/`](operating-method/) | The method I built for running Claude Code as a supervised analyst inside a client's protected data environment, on a real risk-adjustment engagement.  Two instances, one operator, evidence labels on every figure, a QA pass that caught a headline built on a join error, and a document red-team before anything reached the client.  The client is de-identified and the example figures are invented. | [The headline that was an artifact](operating-method/03-qa-gates-and-the-headline-that-was-an-artifact.md), then the [overview](operating-method/README.md) |
| [`order-completion/`](order-completion/) | A working prototype: a text conversation, built from a patient's chart, designed to get a health system's unfilled lab orders completed and closed by the system's own result.  Python, 257 tests, synthetic data only.  The model writes the words; the application owns the facts.  Messaging and booking are simulated.  It has not been run with real patients. | [`docs/technical-brief.md`](order-completion/docs/technical-brief.md) |
| [`working-system/`](working-system/) | How the practice runs: one repository per venture, files as the shared memory between models and sessions, a two-model review protocol, and a ledger of what I may not claim. | [`working-system/README.md`](working-system/README.md) |

## What I did and what the models did

I want this read precisely, so here is the division of labor.

- **In the engagement**, I defined the questions, the operating framework, and the QA gates, and I was the operator inside the client's environment.  Claude executed the SQL and the CMS model code.  The client's analytics team independently validated the analysis.  I did not write the SQL.  I am not an engineer by training.
- **In the prototype**, I set the product constraint: automate the reliable part and hand the rest to a named human.  Measure the human minutes.  I specified each version's behavior, played the patient, flagged what was wrong, and decided what the model is never allowed to decide.  Claude Code wrote the code and the tests.  Codex reviewed each version before the next one started.  The full review record is private.  The version 4 and version 5 reviews are reconciled in included regression tests: [`tests/test_v4.py`](order-completion/tests/test_v4.py) and [`tests/test_v5.py`](order-completion/tests/test_v5.py).
- **In the working system**, I wrote the rules.  Claude Code and Codex follow them.

## The year, in order

| When | What | Where the evidence is |
|---|---|---|
| Oct 2025 – Feb 2026 | Strategy sprint for a private-equity-owned risk-adjustment software company: product kill/keep, a Medicare Advantage ROI analysis from a vendor's suggestion-level data, board deliverables.  Done with ChatGPT, before I adopted Claude Code. | Not included (client work) |
| Oct 2025 – Feb 2026 | Co-developed an AI-native specialty second-opinion venture with a physician co-founder.  Working prototype in a weekend.  We split amicably in February. | Not included |
| Feb – Oct 2026 | Co-founded a diagnostics-access company.  Incorporated, raised none, courted a regional health system, a national retailer, and family-office capital.  Trained as a phlebotomist to understand the work from the chair. | [`working-system/`](working-system/) |
| Spring – summer 2026 | The 60-hour risk-adjustment engagement for a large employed physician group: a six-cut SQL analysis run by a dedicated Claude Code instance inside the client's perimeter, a V28 scoring ladder, a reconciled client report, and a method that survived its own mistakes. | [`operating-method/`](operating-method/) |
| Sep 2026 | Built the order-completion prototype, versions 1 through 5 plus a feed-integrity layer, in nine days, with a Codex review between versions. | [`order-completion/`](order-completion/) |
| Aug – Oct 2026 | Built a local-only finance system and an evidence-based map of fifteen months of work to decide what to do next.  This repository is part of the answer. | [`working-system/`](working-system/) |

## How to read the prototype in ten minutes

Python 3.11.  Mock mode uses only the standard library and needs no credentials.  From the repository root:

```bash
cd order-completion
python3.11 -m unittest discover -s tests -t .     # 257 tests
python3.11 -m ocp demo --quiet                     # 41 scripted synthetic scenarios → PASS/FAIL ledger
python3.11 -m ocp seed                             # fresh interactive synthetic session
python3.11 -m ocp serve                            # the operator's work surface on http://127.0.0.1:8765
```

Open the address and choose an active conversation.  The demo replaces the local synthetic database; seed then resets the conversations for an interactive session.  Then read [`docs/technical-brief.md`](order-completion/docs/technical-brief.md) for what is functional, simulated, partner-dependent, unverified, and not built.  Those five words are used on purpose and are used on screen.

## What is deliberately not here

- Any client's name, data, deliverables, SQL, or figures.  Under my services agreements I own the prompts, methods, and frameworks; the clients own everything else.  `operating-method/` is written against a fictional medical group with illustrative numbers.
- The venture's investor materials, financial model, and partner correspondence.  The prototype's cost model is included with the founder-compensation provision removed.
- My finance system.  The rules are described; the ledger is mine.
- Anything that would identify a patient, a member, or a partner.  The prototype's data is synthetic and says so in the files.

## Contact

Kate Casaday · kate@casadaycg.com · [LinkedIn](https://www.linkedin.com/in/katecasaday/)

See [`NOTICE.md`](NOTICE.md).  Published for review; not open source.
