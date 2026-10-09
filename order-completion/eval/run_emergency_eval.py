#!/usr/bin/env python3
"""Emergency-screen eval: code floor vs. live classifier vs. both, on eval/emergency_cases.py (Set B).
PAID when --adapter anthropic.  Reuses SpendGuard from run_eval.py; stops before any call that could exceed --budget-usd.

    python3 eval/run_emergency_eval.py --adapter mock
    python3 eval/run_emergency_eval.py --adapter anthropic --model claude-opus-5 --budget-usd 0.50 --yes-i-accept-paid-calls
"""
import argparse, json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ocp.rules import emergency_signal
from ocp.llm.mock import MockAdapter
from emergency_cases import CASES
from run_eval import SpendGuard

CTX = {"history": [{"direction": "outbound", "body": "Hi, this is the AI assistant for Dr. Casaday's office. You have a lab order open. "
                    "Riverbend Lab in Bath is open 7am-4pm weekdays. Would a day this week work? Reply STOP to opt out."}],
       "open_order_tests": ["CBC", "Lipid panel"], "agreed_plan": None, "partner_name": "Riverbend Health",
       "site_names": ["Riverbend Lab Bath", "Riverbend Lab Brunswick"], "known_towns": ["Bath", "Brunswick"], "preferences": {}}

def stats(rows, pred):
    tp = sum(1 for r in rows if r["label"] == "E" and pred(r)); fn = sum(1 for r in rows if r["label"] == "E" and not pred(r))
    fp = sum(1 for r in rows if r["label"] == "N" and pred(r)); tn = sum(1 for r in rows if r["label"] == "N" and not pred(r))
    return "sens %d/%d=%.0f%%  spec %d/%d=%.0f%%" % (tp, tp + fn, 100 * tp / max(1, tp + fn), tn, tn + fp, 100 * tn / max(1, tn + fp))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", choices=["mock", "anthropic"], default="mock")
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--budget-usd", type=float, default=0.50)
    ap.add_argument("--max-calls", type=int, default=60)
    ap.add_argument("--max-output-tokens", type=int, default=200)
    ap.add_argument("--yes-i-accept-paid-calls", action="store_true")
    a = ap.parse_args()
    if a.adapter == "anthropic":
        if not a.yes_i_accept_paid_calls: sys.exit("refusing: paid calls need --yes-i-accept-paid-calls")
        if not os.environ.get("ANTHROPIC_API_KEY"): sys.exit("credential error: ANTHROPIC_API_KEY not set (bin/run.sh loads it from the Keychain)")
        from ocp.llm.anthropic_adapter import AnthropicAdapter
        adapter = AnthropicAdapter(model=a.model, max_tokens=a.max_output_tokens)
    else:
        adapter = MockAdapter()
    guard = SpendGuard(a.budget_usd, a.max_calls, a.max_output_tokens, a.model)
    rows = []
    for label, kind, text in CASES:
        r = {"label": label, "kind": kind, "text": text, "floor": emergency_signal(text), "intent": None, "model": None, "error": None}
        ok, why = guard.allow(CTX, text, 1)
        if not ok:
            r["error"] = "skipped: " + why
        else:
            try:
                res = adapter.classify(CTX, text, budget=1)
                r["intent"], r["confidence"] = res.intent, res.confidence
                if a.adapter == "anthropic": guard.record(res)
            except Exception as e:
                r["error"] = str(e)[:200]
                if a.adapter == "anthropic": guard.record_attempts(getattr(adapter, "last_attempts", None) or [])
        r["model"] = r["intent"] == "emergency"
        rows.append(r)
    out = {"adapter": a.adapter, "model": a.model if a.adapter == "anthropic" else "mock-rules-v1", "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "calls": guard.calls, "spent_usd": round(guard.spent, 4),
           "floor": stats(rows, lambda r: r["floor"]), "model_only": stats(rows, lambda r: r["model"]),
           "floor_or_model": stats(rows, lambda r: r["floor"] or r["model"]), "rows": rows}
    Path("eval/results").mkdir(exist_ok=True)
    fn = "eval/results/emergency-%s-%s.json" % (out["model"], time.strftime("%Y%m%dT%H%M%S"))
    Path(fn).write_text(json.dumps(out, indent=1))
    print("model=%s  calls=%d  spent=$%.4f" % (out["model"], out["calls"], out["spent_usd"]))
    print("floor only     :", out["floor"]); print("model only     :", out["model_only"]); print("floor OR model :", out["floor_or_model"])
    print("\nMISSED by both:"); [print("  [%s] %s  -> %s" % (r["kind"], r["text"], r["intent"])) for r in rows if r["label"] == "E" and not (r["floor"] or r["model"])]
    print("\nFALSE ALARM by model:"); [print("  [%s] %s" % (r["kind"], r["text"])) for r in rows if r["label"] == "N" and r["model"]]
    print("\nERRORS/SKIPPED:"); [print("  %s: %s" % (r["text"][:50], r["error"])) for r in rows if r["error"]]
    print("\nsaved", fn)

if __name__ == "__main__": main()
