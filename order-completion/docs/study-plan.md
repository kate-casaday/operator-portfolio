# Walking the prototype – a study plan for Kate

*Written Sept 21, 2026 by Claude Code, from `technical-brief.md` and `architecture-v5.md`.  Purpose: Kate can explain every design decision in this build out loud, in an engineer's vocabulary, and say what she would change.  The goal is fluency in the ideas, not line-by-line recall.  Ten sessions of about ninety minutes.*

**How each session runs.**  (1) Claude walks one subsystem with the real code open.  (2) Kate explains it back in her own words, as if to a staff engineer.  (3) Claude asks the three questions an engineer would ask: what breaks it, what it costs, what you would do differently at 100x the load.  (4) Kate writes five lines in her own words at the bottom of this file.  Those notes are the deliverable.

| # | Subsystem | Where | The engineering ideas it teaches |
|---|---|---|---|
| 1 | **The shape: who decides what** | brief §2; `engine.py`, `rules.py` | Separation of concerns; deterministic code vs. a probabilistic model; why state changes, consent and completion never belong to the model; "orchestrator" and "authority" as roles |
| 2 | **The data model** | `db.py`; schema in `data/schema/` | Relational tables; append-only event logs vs. mutable rows; idempotent imports; stable identifiers; why "order history" is events, not a status field |
| 3 | **The inbound pipeline** | `engine.py` inbound path | Ordered screens (STOP, HELP, wrong number, emergency) before any model call; commit-before-process so a crash cannot lose a message; exactly-once handling; binding a reply to its original sender |
| 4 | **Fact sheets and the fact check** | `facts.py`; composer; brief §1 (v3) | Grounding; allowlists vs. blocklists; why the check is *lexical, not semantic* and what that does and does not prove; fallbacks to approved templates; provenance and role-gated approval |
| 5 | **The model layer** | `llm/`, `models.py` | Adapters and interfaces (mock, Anthropic, routed); closed-vocabulary classification; confidence floors; prompt and output contracts; how you would swap a model without touching the engine |
| 6 | **Dead ends: resolver and reviewer** | brief §1 (v4) | Constrained choice from a verified option menu; an independent second model as a check; when disagreement escalates to a person; why "two models agree" is evidence and not proof |
| 7 | **Time, queues and cancellation** | tick loop; outbox; epochs (brief §5) | Schedulers; outbox pattern; epochs as a way to cancel queued work safely; quiet hours; stale-feed pause; what concurrency problems real webhooks would add |
| 8 | **Controls and cost** | brief §5; `costs/` | Kill switches and pauses at enqueue and at flush; rate and spend limits (the 24-hour brake); audit events; the rent-versus-self-host arithmetic and why model tokens are a rounding error next to SMS and human minutes |
| 9 | **Evaluation and change control** | `improve.py`, `feedback.py`; tests | Held-out sets and why 17 of 19 on synthetic replies is not accuracy on real language; regression cases from flagged conversations; versioned releases and rollback *records* vs. deployed rollback; the automated tests and 41 scripted scenarios |
| 10 | **What is not built, and the path to production** | brief §6–8; `architecture-v5.md` assertions audit | Authentication, secrets, hosting under a business associate agreement, delivery receipts, signature validation, identity beyond a phone number, observability; how to talk about a prototype honestly |

**Two threads that run through all ten.**  *Safety by construction* – the pattern of letting a model write only inside facts the application owns, with code-level floors underneath.  *Review by a second model* – what Codex caught that Claude missed across five versions and on Sept 21, and what that says about how to supervise AI-written code you cannot fully read.

**A good stopping test.**  Kate can answer, without notes: why does the model never decide that an order is complete?  What exactly does the fact check prove?  What happens to a queued text when a patient replies STOP a second before it sends?  What would you rebuild first with a real engineer, and why?

