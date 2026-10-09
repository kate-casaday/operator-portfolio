#!/usr/bin/env python3
"""Wording evaluation: does the composer write what Kate's voice principles ask for, within the facts?

    .venv/bin/python eval/run_compose_eval.py --composer fact                       # deterministic writer, no cost
    .venv/bin/python eval/run_compose_eval.py --composer anthropic --model claude-opus-5 \
        --budget-usd 3.00 --max-calls 40 --yes-i-accept-paid-calls                  # PAID

Each case replays a synthetic thread through the REAL engine (mock classifier, so the application's decision is
deterministic) with the chosen composer writing the words.  Checks: the fact check passed (the engine sent the
composed text, not the template fallback), plus the case's expectations.  Every composed text is written to
eval/results/<timestamp>-compose-<composer>.json and .md so Kate can read the wording, not just the score.

Spend guard: the engine's own per-day cap is set to --budget-usd for the run, and the runner stops when the
recorded live spend reaches it.  Actual usage is read back from model_calls after every case.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tests.helpers import make_engine  # noqa: E402
from ocp.db import rows, row  # noqa: E402
from ocp.llm.composer import build_composer  # noqa: E402
from ocp.metrics import sms_segments  # noqa: E402
from ocp.rules import Policy  # noqa: E402
from ocp.scenarios import P as PHONE  # noqa: E402


def check(case, m, body):
    exp = case["expect"]
    why = []
    if m["composer"] == "template":
        why.append("composer output refused or failed; template fallback was sent")
    if "template" in exp and m["template_id"] != exp["template"]:
        why.append("template %s != %s" % (m["template_id"], exp["template"]))
    if "template_in" in exp and m["template_id"] not in exp["template_in"]:
        why.append("template %s not in %s" % (m["template_id"], exp["template_in"]))
    low = body.lower()
    if exp.get("must_contain_any") and not any(x.lower() in low for x in exp["must_contain_any"]):
        why.append("none of %s present" % exp["must_contain_any"])
    for x in exp.get("must_contain_all", []):
        if x.lower() not in low:
            why.append("missing %r" % x)
    for x in exp.get("must_not_contain", []):
        if x.lower() in low:
            why.append("contains %r" % x)
    if "max_questions" in exp and body.count("?") > exp["max_questions"]:
        why.append("%d questions > %d" % (body.count("?"), exp["max_questions"]))
    if "max_segments" in exp and sms_segments(body) > exp["max_segments"]:
        why.append("%d segments > %d" % (sms_segments(body), exp["max_segments"]))
    if exp.get("stop_footer") and not body.rstrip().endswith("Reply STOP to opt out."):
        why.append("no STOP footer")
    if exp.get("ack_words") and not any(w.lower() in low[:120] for w in exp["ack_words"]):
        why.append("no acknowledgement near the start (%s)" % exp["ack_words"])
    return why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--composer", default="fact", choices=["fact", "anthropic"])
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--budget-usd", type=float, default=1.00)
    ap.add_argument("--max-calls", type=int, default=40)
    ap.add_argument("--only", default="", help="comma-separated case ids")
    ap.add_argument("--yes-i-accept-paid-calls", action="store_true")
    args = ap.parse_args()
    live = args.composer == "anthropic"
    if live and not args.yes_i_accept_paid_calls:
        print("refusing: --composer anthropic makes PAID calls; pass --yes-i-accept-paid-calls"); return 2
    cases = json.load(open(os.path.join(HERE, "compose_cases.json")))["cases"]
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    results, spent, calls = [], 0.0, 0
    for case in cases:
        if live and (spent >= args.budget_usd or calls >= args.max_calls):
            results.append({"id": case["id"], "status": "skipped", "why": "budget or call cap reached"}); continue
        eng = make_engine(policy=Policy(max_spend_usd_per_day=max(0.01, args.budget_usd - spent)), tick=False)
        eng.composer = build_composer("fact")
        eng.tick()                                   # the 20 openers are written by the deterministic writer: only this case's turns are paid
        if live:
            eng.composer = build_composer(args.composer, model=args.model, effort=args.effort)
        last = None
        for i, turn in enumerate(case["turns"]):
            last = eng.handle_inbound(PHONE[case["patient"]], turn, "%s-%d" % (case["id"], i))
        cid = row(eng.conn, "SELECT c.id FROM conversations c JOIN patients p ON p.id=c.patient_id WHERE p.source_patient_id=?", ("P-%02d" % case["patient"],))["id"]
        m = rows(eng.conn, "SELECT * FROM messages WHERE conversation_id=? AND direction='outbound' AND status IN ('sent','queued') ORDER BY id DESC LIMIT 1", (cid,))[0]
        mc = row(eng.conn, "SELECT COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM model_calls WHERE simulated=0 AND purpose='compose'") or {}
        spent += float(mc.get("c") or 0); calls += int(mc.get("n") or 0)
        refusals = [json.loads(e["detail"]) for e in rows(eng.conn, "SELECT detail FROM events WHERE kind='composer_refused' AND conversation_id=?", (cid,))]
        why = check(case, m, m["body"])
        thread = rows(eng.conn, "SELECT direction, body FROM messages WHERE conversation_id=? AND status NOT IN ('cancelled','suppressed') ORDER BY id", (cid,))
        results.append({"id": case["id"], "title": case["title"], "status": "pass" if not why else "fail", "why": "; ".join(why),
                        "template": m["template_id"], "composer": m["composer"], "text": m["body"], "segments": sms_segments(m["body"]),
                        "refusals": refusals, "thread": thread, "spend_usd_so_far": round(spent, 4), "intent": last.get("intent") if last else None})
        print("[%s] %s  %s%s" % ("PASS" if not why else "FAIL", case["id"], case["title"], ("\n       -> " + "; ".join(why)) if why else ""))
        print("       " + m["body"].replace("\n", " ") + "\n")
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    label = ("LIVE COMPOSER %s (%s)" % (args.model, args.effort)) if live else "DETERMINISTIC FACT WRITER (not a model)"
    summary = {"label": label, "composer": args.composer, "model": args.model if live else "fact-composer-v1", "cases": len(cases),
               "passed": sum(1 for r in results if r["status"] == "pass"), "failed": sum(1 for r in results if r["status"] == "fail"),
               "skipped": sum(1 for r in results if r["status"] == "skipped"), "spend_usd": round(spent, 4), "compose_calls": calls}
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    base = os.path.join(HERE, "results", "%s-compose-%s" % (ts, args.composer))
    json.dump({"summary": summary, "results": results}, open(base + ".json", "w"), indent=1)
    with open(base + ".md", "w") as f:
        f.write("# Wording evaluation - %s\n\n%d/%d pass · spend $%.4f · %d compose calls\n\n" % (label, summary["passed"], summary["cases"], spent, calls))
        for r in results:
            if r["status"] == "skipped":
                f.write("## %s - SKIPPED (%s)\n\n" % (r["id"], r["why"])); continue
            f.write("## %s - %s - %s\n\n" % (r["id"], r["status"].upper(), r["title"]))
            for t in r["thread"]:
                f.write("- **%s:** %s\n" % ("APP" if t["direction"] == "outbound" else "PATIENT", t["body"]))
            f.write("\n*writer: %s · template: %s · %d segments*%s\n\n" % (r["composer"], r["template"], r["segments"], ("  \n**why failed:** " + r["why"]) if r["why"] else ""))
            for rf in r["refusals"]:
                f.write("> fact-check refusal (attempt %s): %s  \n> rejected text: %s\n\n" % (rf.get("attempt"), "; ".join(rf.get("violations", [])), rf.get("rejected_text")))
    print("== %s ==\ncases=%d pass=%d fail=%d skipped=%d spend=$%.4f calls=%d\nwritten: %s.md" % (label, summary["cases"], summary["passed"], summary["failed"], summary["skipped"], spent, calls, os.path.relpath(base, ROOT)))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
