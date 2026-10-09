#!/usr/bin/env python3
"""Conversation evaluation runner.

    python3 eval/run_eval.py --adapter mock --set dev          # rule-based SIMULATION results (not model accuracy)
    python3 eval/run_eval.py --adapter mock --set heldout
    python3 eval/run_eval.py --adapter anthropic --set heldout --budget-usd 1.00 --max-calls 20 --max-output-tokens 200

The live run (adapter anthropic) was NOT executed in round two.  It requires `pip install anthropic` and a
credential; it makes PAID calls.  Spend guard: before every call the runner estimates the worst-case cost of
that call (conservative input estimate = chars/3 + system prompt, output = --max-output-tokens at the model's
output price) and STOPS if cumulative actual spend + that estimate would exceed --budget-usd.  This is not a
hard dollar ceiling: actual input tokens are known only after the call, so a single call can overshoot the
estimate; the overshoot is bounded by the difference between the estimate and the real prompt size (the
output side is capped by max_tokens).  Every call's real usage and every failure are recorded in
eval/results/<timestamp>-<adapter>-<set>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ocp.llm import build_adapter  # noqa: E402
from ocp.llm.base import ProviderError, SYSTEM_PROMPT, INTENT_SCHEMA, ModelAdapter  # noqa: E402
from ocp.rules import validate_model_output, prescreen_inbound, Policy  # noqa: E402
from ocp.models import INTENTS  # noqa: E402

PRICES = json.load(open(os.path.join(ROOT, "costs", "prices.json")))
DIRECTORY = json.load(open(os.path.join(ROOT, "data", "partner_directory.json")))
PARTNER_CTX = {"partner_name": DIRECTORY["partner_name"], "site_names": [s["name"] for s in DIRECTORY["sites"]],
               "known_towns": sorted({s["town"] for s in DIRECTORY["sites"]})}


def load_cases(which):
    path = os.path.join(HERE, "%s_cases.json" % ("dev" if which == "dev" else "heldout"))
    return json.load(open(path))["cases"]


def matches(expect, out):
    """expect: intent | intent_in | not_intent; constraints (subset) | constraints_any (at least one)."""
    ok, why = True, []
    if "intent" in expect and out["intent"] != expect["intent"]:
        ok = False; why.append("intent %s != %s" % (out["intent"], expect["intent"]))
    if "intent_in" in expect and out["intent"] not in expect["intent_in"]:
        ok = False; why.append("intent %s not in %s" % (out["intent"], expect["intent_in"]))
    if "not_intent" in expect and out["intent"] == expect["not_intent"]:
        ok = False; why.append("intent must not be %s" % expect["not_intent"])
    for k, v in (expect.get("constraints") or {}).items():
        if out["constraints"].get(k) != v:
            ok = False; why.append("constraint %s=%r (want %r)" % (k, out["constraints"].get(k), v))
    if expect.get("constraints_any") and not any(out["constraints"].get(k) == v for k, v in expect["constraints_any"].items()):
        ok = False; why.append("none of %s present" % expect["constraints_any"])
    return ok, "; ".join(why)


class SpendGuard:
    def __init__(self, budget_usd, max_calls, max_output_tokens, model_id):
        self.budget, self.max_calls, self.max_out = budget_usd, max_calls, max_output_tokens
        self.price = PRICES["models"].get(model_id) or PRICES["models"]["claude-opus-5"]
        self.spent, self.calls = 0.0, 0

    def estimate(self, ctx, text):
        """Worst-case cost of one call: the ACTUAL serialized request (system prompt + full user content built by
        ModelAdapter.build_messages + the JSON schema) at chars/3, plus max output tokens at the output price."""
        user = ModelAdapter.build_messages(ctx, text)[0]["content"]
        est_in = (len(SYSTEM_PROMPT) + len(user) + len(json.dumps(INTENT_SCHEMA)) + 200) / 3.0
        return (est_in * self.price["input"] + self.max_out * self.price["output"]) / 1e6

    def allow(self, ctx, text, requests_per_invocation=1):
        if self.calls + requests_per_invocation > self.max_calls:
            return False, "max calls would be exceeded (%d used of %d; next invocation may make %d requests)" % (self.calls, self.max_calls, requests_per_invocation)
        est = self.estimate(ctx, text) * requests_per_invocation      # routed: reserve for the fallback request too
        if self.spent + est > self.budget:
            return False, "next call could exceed budget: spent $%.4f + est $%.4f > $%.2f" % (self.spent, est, self.budget)
        return True, ""

    def record_attempts(self, attempts):
        """Every attempt (ok or error) with known usage counts toward spend; unknown usage is recorded as unknown."""
        known = []
        for a in attempts:
            price = PRICES["models"].get(a.get("model") or "", self.price)    # price each attempt at its own model
            if a.get("input_tokens") is not None:
                self.spent += ((a.get("input_tokens") or 0) * price["input"] + (a.get("cache_read_tokens") or 0) * price.get("cache_read", 0)
                               + (a.get("output_tokens") or 0) * price["output"]) / 1e6
                known.append(True)
            else:
                known.append(False)
        self.calls += max(1, len(attempts))
        return known

    def record(self, r):
        self.calls += 1
        self.spent += (r.input_tokens * self.price["input"] + r.cache_read_tokens * self.price.get("cache_read", 0)
                       + r.output_tokens * self.price["output"]) / 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default="mock", choices=["mock", "anthropic", "routed"])
    ap.add_argument("--set", default="dev", choices=["dev", "heldout"])
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--budget-usd", type=float, default=1.00)
    ap.add_argument("--max-calls", type=int, default=25)
    ap.add_argument("--max-output-tokens", type=int, default=200)
    ap.add_argument("--yes-i-accept-paid-calls", action="store_true", help="required for adapter anthropic/routed")
    args = ap.parse_args()
    cases = load_cases(args.set)
    live = args.adapter != "mock"
    if live and not args.yes_i_accept_paid_calls:
        print("Refusing: live adapter makes PAID calls.  Re-run with --yes-i-accept-paid-calls.", file=sys.stderr)
        return 2
    if args.adapter == "anthropic":
        adapter = build_adapter("anthropic", model=args.model, max_tokens=args.max_output_tokens, max_retries=0)
    elif args.adapter == "routed":
        adapter = build_adapter("routed", strong_model=args.model, max_tokens=args.max_output_tokens, max_retries=0)
    else:
        adapter = build_adapter("mock")
    per_call_budget = 2 if args.adapter == "routed" else 1     # routed needs one attempt for the fallback
    guard = SpendGuard(args.budget_usd, args.max_calls, args.max_output_tokens, args.model)
    label = "RULE-BASED SIMULATION (mock adapter; NOT model accuracy)" if not live else "LIVE MODEL RESULTS (%s)" % args.model
    results, passed, skipped = [], 0, 0
    for c in cases:
        ctx = dict(PARTNER_CTX, history=[{"direction": "outbound", "body": "Riverbend Health: Hi, this is an automated text service. Your care team has lab work that is still open. Reply with a day that works, a question, or STOP."}],
                   open_order_tests=["Complete blood count"], agreed_plan=None, preferences={})
        rec = {"id": c["id"], "text": c["text"], "expect": c["expect"], "tags": c.get("tags", [])}
        if live:
            ok, why = guard.allow(ctx, c["text"], per_call_budget)
            if not ok:
                rec.update(status="skipped", reason=why); skipped += 1; results.append(rec); continue
        t0 = time.perf_counter()
        hard = prescreen_inbound(c["text"], Policy()).hard_intent
        if hard:
            # STOP / HELP / wrong number are decided by code before any model sees the text (same as the engine).
            out = {"intent": hard, "confidence": 1.0, "barrier": "none", "constraints": {}}
            ok, why = matches(c["expect"], out)
            rec.update(status="pass" if ok else "fail", got=out, why=why, decided_by="rule", simulated=True)
            passed += 1 if ok else 0
            results.append(rec)
            continue
        try:
            r = adapter.classify(ctx, c["text"], budget=per_call_budget)
            out = validate_model_output({"intent": r.intent, "confidence": r.confidence, "barrier": r.barrier, "constraints": r.constraints})
            attempts = list(getattr(adapter, "last_attempts", None) or [])
            if live:
                guard.record_attempts(attempts)
            rec["attempts"] = attempts
            ok, why = matches(c["expect"], out)
            rec.update(status="pass" if ok else "fail", got=out, why=why, simulated=r.simulated,
                       usage={"input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "cache_read_tokens": r.cache_read_tokens},
                       latency_ms=round((time.perf_counter() - t0) * 1000, 1))
            passed += 1 if ok else 0
        except ProviderError as e:
            attempts = list(getattr(adapter, "last_attempts", None) or [])
            known = guard.record_attempts(attempts) if live else []
            rec.update(status="error", error=str(e), attempts=attempts,
                       usage={"known": all(known) if known else False,
                              "input_tokens": sum(a.get("input_tokens") or 0 for a in attempts) or None,
                              "output_tokens": sum(a.get("output_tokens") or 0 for a in attempts) or None,
                              "cache_read_tokens": sum(a.get("cache_read_tokens") or 0 for a in attempts) or None},
                       model=attempts[-1].get("model") if attempts else None)
        results.append(rec)
    summary = {"label": label, "adapter": args.adapter, "model": args.model if live else "mock-rules-v1", "set": args.set,
               "cases": len(cases), "passed": passed, "failed": sum(1 for r in results if r["status"] == "fail"),
               "errors": sum(1 for r in results if r["status"] == "error"), "skipped_by_guard": skipped,
               "spend_usd_actual": round(guard.spent, 4) if live else 0.0, "budget_usd": args.budget_usd if live else None,
               "intents_in_vocabulary": INTENTS, "ran_at": datetime.now().isoformat(timespec="seconds")}
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    out_path = os.path.join(HERE, "results", "%s-%s-%s.json" % (datetime.now().strftime("%Y%m%dT%H%M%S"), args.adapter, args.set))
    with open(out_path, "w") as f:
        json.dump({"summary": summary, "results": results}, f, indent=2)
    print("== %s ==" % label)
    print("set=%s  cases=%d  pass=%d  fail=%d  error=%d  skipped=%d  spend=$%.4f" % (
        args.set, len(cases), passed, summary["failed"], summary["errors"], skipped, guard.spent))
    for r in results:
        if r["status"] != "pass":
            print("  [%s] %s: %r -> %s" % (r["status"].upper(), r["id"], r["text"][:60], r.get("why") or r.get("reason") or r.get("error")))
    print("written:", os.path.relpath(out_path, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
