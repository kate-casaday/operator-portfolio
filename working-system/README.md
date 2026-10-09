# How I work: the repo is the operating system

Since early 2026 every piece of my professional life has run out of a set of private git repositories, with Claude Code as the operator's tool inside each one.  This section describes the pattern, because the pattern is the thing that transfers.  The two exhibits beside it (`order-completion/`, `operating-method/`) are what the pattern produced.

## The shape

```
one repo per venture or engagement          life-os/  ← the layer above all of them
  CLAUDE.md      what the company is, what is true today, what the model may not claim
  AGENTS.md      the evidence protocol every model follows (Claude Code and Codex read the same file)
  WEEK.md        this week's work, rewritten at session end
  THREADS.md     who owes whom, one line per open thread
  meetings/      transcripts + derivative notes, dated
  crm/           relationship cards, call prep, call notes
  strategy/      the documents, each with a status ladder: draft → review1 → review2 → final → sent
  sessions/      a dated handoff written at the end of every working session
  inbox/         anything captured on a phone lands here and is routed at the next session start
```

Seven repositories, roughly 690 commits between January and October 2026.  Every commit was made in a Claude Code session with me in the chair.

## The rules that make it work

1. **Files are the shared memory.**  No model sees another model's session, and no session sees the last one.  Anything that matters is written down, dated, and committed.  "As we discussed" is not evidence.
2. **A status block at the top of `CLAUDE.md` says what is true today.**  Older status blocks stay underneath as history.  A model opening the repo cold reads the top block and knows where things stand.
3. **Every claim traces to a transcript, a file, or is labeled unverified.**  The `AGENTS.md` evidence protocol: read the week and the threads, read the derivative notes, verify exact wording against the raw transcript, search the meeting tool if the repo is silent, and label anything that cannot be traced.
4. **The model never sends.**  Investor, partner, client, and counsel material goes out from me.  Drafts carry a status; nothing outward-facing ships with an open review.
5. **Two models, one owner.**  Substantive documents get an author from one lab and a reviewer from the other, alternating in an append-only review log, with disagreements escalated to me rather than to a third model.  The protocol is in `dual-model-protocol.md` in this directory.
6. **A "what I should not claim" ledger.**  The career-decision repo keeps an explicit list of things the evidence does not support, next to the things it does.  Every resume line and every interview answer is checked against it.  The same discipline runs through client work (evidence labels, retired-figure lists) and the prototype (status words: functional, simulated, partner-dependent, unverified, not built).
7. **Dashboards, not duplicates.**  The layer above the ventures (`life-os`) holds one line per venture and a link.  Task lists live in the venture that owns them.  Any session that touches a venture updates the dashboard at the end.

## Two smaller systems built the same way

**A personal and small-business finance system** (local-only, zero third-party dependencies, 31 tests).  Statement PDFs are checksummed and archived, parsed into one canonical ledger, classified by rules in version control, and reported with as-of dates and coverage caveats.  Its non-negotiables: never commit a raw source, never delete an original, never store a full account number, never call an external API, never resolve an accounting judgment the owner or the accountant should make, and keep every import idempotent so a re-run creates zero duplicates.  Not included here because it is my money, but the rules are the point.

**A work map.**  When I needed to decide what to do next with my career, I had Claude Code build an evidence-based map of fifteen months of work from three sources: the repositories, about 259 meeting transcripts, and sent mail.  The map, not my memory, is what the decision was made against.

## What this is not

It is not an autonomous agent.  I am in every session.  The model does the reading, the drafting, the SQL, the code, and the reconciliation.  I decide what question is worth asking, what the model cannot see, what is true, and what goes out the door.
