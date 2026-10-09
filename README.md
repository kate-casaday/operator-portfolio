# Kate Casaday: a year of building with Claude Code

I am a healthcare operator.  I ran risk-adjustment and value-based-care operations inside a large medical group, built the revenue system for a healthcare services company, and since October 2025 have run my own consulting practice and co-founded a healthcare company.  For the last year, every one of those has been built and run with Claude Code as the working tool.  This repository is the evidence.

Three things are here.  Everything is real, nothing is a demo built for this page, and no client, patient, or partner data appears anywhere.

| Section | What it shows | Read first |
|---|---|---|
| [`order-completion/`](order-completion/) | A working prototype I built in September 2026: a text conversation, built from a patient's chart, that gets a health system's unfilled lab orders completed and closed by the system's own result.  About 9,300 lines of Python, 257 tests, synthetic data only.  The model writes the words; the application owns the facts. | [`order-completion/docs/technical-brief.md`](order-completion/docs/technical-brief.md) |
| [`operating-method/`](operating-method/) | The method I built for running Claude Code as a supervised analyst inside a client's protected data environment on a real risk-adjustment engagement.  Two instances, one operator, evidence labels on every figure, a QA pass that caught a fabricated headline, and a document red-team before anything reached the client.  The client is de-identified. | [`operating-method/README.md`](operating-method/README.md) |
| [`working-system/`](working-system/) | How the whole practice runs: one repository per venture, files as the shared memory between models and sessions, a two-model review protocol, and a ledger of what I may not claim. | [`working-system/README.md`](working-system/README.md) |

## What I did and what the model did

I want this to be read precisely, so here is the division of labor.

- **In the prototype**, I set the product constraint ("automate the reliable part, hand the rest to a named human, measure the human minutes"), specified every version's behavior, played the patient, flagged what was wrong, and decided what the model is never allowed to decide.  Claude Code wrote the code and the tests.  OpenAI's Codex independently reviewed every version before the next one started, and every finding was reconciled with a regression test.
- **In the client engagement**, I defined the questions, the operating framework, and the QA gates, and I was the operator inside the client's environment.  Claude executed the SQL and the CMS model code.  The client's analytics team independently validated the analysis.  I did not write the SQL, and I am not an engineer by training.
- **In the working system**, I wrote the rules.  Claude Code and Codex follow them.

## The year, in order

| When | What | Where the evidence is |
|---|---|---|
| Oct 2025 – Feb 2026 | Four-week strategy sprint for a private-equity-owned risk-adjustment software company: product kill/keep, a Medicare Advantage ROI analysis from a vendor's suggestion-level data, board deliverables.  First engagement where Claude did the analytical work and I did the operating. | Not included (confidential) |
| Oct 2025 – Feb 2026 | Co-developed an AI-native specialty second-opinion venture with a physician co-founder; working prototype in a weekend; pitched a venture fund.  We split amicably in February. | Not included |
| Feb – Oct 2026 | Co-founded a diagnostics-access company.  Incorporated, raised none, courted a regional health system, a national retailer, and family-office capital.  Trained as a phlebotomist to understand the work from the chair.  The company's repository is the fullest example of the working system: 280 commits, two models, one owner. | `working-system/` |
| May – Aug 2026 | The 60-hour risk-adjustment engagement for a large employed physician group: a six-cut SQL analysis run by a dedicated Claude Code instance inside the client's perimeter, a V28 scoring ladder, a reconciled client report, and a method that survived its own mistakes. | `operating-method/` |
| Feb – Aug 2026 | Three further engagements: go-to-market architecture for a medical-device company's remote-monitoring product and enterprise-value diligence on a quality-analytics firm, both through a partner firm, and a diligence memo for a family office.  Each ran out of its own folder under the same rules. | Not included (confidential) |
| Sep 2026 | Built the order-completion prototype, versions 1 through 5 plus a feed-integrity layer, in nine days, with a Codex review between every version. | `order-completion/` |
| Aug – Oct 2026 | Built a local-only finance system and a fifteen-month evidence-based work map to decide what to do next.  This repository is part of the answer. | `working-system/` |

Across seven private repositories: roughly 690 commits between January and October 2026.

## How to read the prototype in ten minutes

```bash
cd order-completion
python3.11 -m unittest discover -s tests -t .     # 257 tests, standard library only
python3.11 -m ocp demo                             # 41 scripted synthetic scenarios → PASS/FAIL ledger
python3.11 -m ocp serve                            # the operator's work surface on http://127.0.0.1:8765, mock mode, no key
```

Then read [`docs/technical-brief.md`](order-completion/docs/technical-brief.md) for what is functional, simulated, partner-dependent, unverified, and not built.  Those five words are used on purpose and are used on screen.

## What is deliberately not here

- Any client's name, data, deliverables, SQL, or figures.  Under my services agreements I own the prompts, methods, and frameworks; the clients own everything else.  `operating-method/` is written against a fictional medical group with illustrative numbers.
- The company's investor materials, financial model, and partner correspondence.
- My finance system.  The rules are described; the ledger is mine.
- Anything that would identify a patient, a member, or a partner.  The prototype's data is synthetic and says so in the files.

## Contact

Kate Casaday · kate@casadaycg.com · [LinkedIn](https://www.linkedin.com/in/katecasaday/)

See [`NOTICE.md`](NOTICE.md).  Published for review; not open source.
