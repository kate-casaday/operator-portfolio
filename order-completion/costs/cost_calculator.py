#!/usr/bin/env python3
"""Reproducible monthly cost calculator for 100 / 1,000 / 10,000 enrolled patients.

    python3 costs/cost_calculator.py            # prints markdown, writes costs/results.md
    python3 costs/cost_calculator.py --json     # machine-readable

Options compared:
  A. Hosted model API baseline — Claude Opus 5 for every classification call, billed uncached (prefix below minimum).
  B. Selective routing — Claude Haiku 4.5 first; Claude Opus 5 fallback on low confidence / provider error.
  C. Self-hosted open-weight — DeepSeek-V4-Flash on rented GPUs, 24/7, plus MLOps maintenance time.

Every input is either a vendor price (see prices.json, with source + date) or a labeled assumption.
Workload assumptions were taken from the synthetic demo where a measured ratio exists; those ratios
come from scripted fixtures and are NOT evidence of real patient behavior.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PRICES = json.load(open(os.path.join(HERE, "prices.json")))
DEMO_DB = os.path.join(os.path.dirname(HERE), "var", "ocp.sqlite")

# ---------------------------------------------------------------- workload assumptions (per enrolled patient per month)
WORKLOAD = {
    "outbound_msgs_per_patient": 3.5,      # [A] initial + ~2 follow-ups/replies; demo measured 3.9 sent per conversation over ~3 sim-weeks
    "inbound_msgs_per_patient": 1.2,       # [A] demo fixtures over-represent replies (all scripted); real reply rates unknown
    "segments_per_outbound": 1.5,          # [demo-measured] 83 segments / 55 outbound messages in the synthetic run
    "segments_per_inbound": 1.0,           # [demo-measured]
    "model_calls_per_inbound": 1.0,        # [rule] one classification per inbound; hard intents (STOP/HELP) skip the model
    "input_tokens_per_call": 900,          # [A] system prompt ~350 + this patient's history/orders ~550 (new tokenizer, +30%)
    "cached_prefix_tokens": 350,           # [A] size of the stable system prompt (only matters in the A-cache sensitivity row)
    "output_tokens_per_call": 80,          # [A] JSON intent object
    "cache_hit_rate": 0.0,                 # [rule] baseline is UNCACHED: the ~350-token system prefix is below the documented
                                           #        minimum cacheable size (512 Opus 5 / 1,024 Sonnet 5 / 4,096 Haiku 4.5, per
                                           #        platform.claude.com prompt-caching docs read 2026-09-15); see sensitivity row
    "fallback_rate_option_b": 0.25,        # [A] share of Haiku calls re-run on Opus (low confidence or error)
    "kate_escalations_per_patient": 0.20,  # [A] demo: 6 of 14 conversations escalated, but fixtures were chosen to be hard
    "minutes_per_escalation": 5.0,         # [A] triage + one reply/booking; demo default minutes range 2-8
    "clinician_escalations_per_patient": 0.05,  # [A] partner clinician time, reported but not costed to StealthCo
    "oversight_minutes_per_day": 20.0,     # [A] dashboard review, feed check; fixed per partner, not per patient
    "days_per_month": 30,
    "gpu_hours_per_month": 24 * 30,        # [rule] 24/7 because inbound texts arrive at any hour
    "mlops_fte_fraction_option_c": 0.25,   # [A] model serving upkeep: upgrades, monitoring, evals, on-call
    "engineer_month_loaded": PRICES["development_one_time"]["engineer_month_loaded"],
}


def demo_measured():
    """Read ratios from the last synthetic demo run, if present, so the table can show them next to assumptions."""
    if not os.path.exists(DEMO_DB):
        return None
    c = sqlite3.connect(DEMO_DB)
    q = lambda sql: c.execute(sql).fetchone()[0] or 0
    out_msg = q("SELECT COUNT(*) FROM messages WHERE direction='outbound' AND status IN ('sent','delivered')")
    out_seg = q("SELECT SUM(segments) FROM messages WHERE direction='outbound' AND status IN ('sent','delivered')")
    in_msg = q("SELECT COUNT(*) FROM messages WHERE direction='inbound'")
    calls = q("SELECT COUNT(*) FROM model_calls")
    convs = q("SELECT COUNT(*) FROM conversations")
    esc = q("SELECT COUNT(*) FROM escalations WHERE queue='kate'")
    tok_in = q("SELECT AVG(input_tokens) FROM model_calls WHERE input_tokens>0")
    return {"conversations": convs, "outbound_per_conversation": round(out_msg / convs, 2) if convs else None,
            "segments_per_outbound": round(out_seg / out_msg, 2) if out_msg else None,
            "inbound_per_conversation": round(in_msg / convs, 2) if convs else None,
            "model_calls_per_inbound": round(calls / in_msg, 2) if in_msg else None,
            "kate_escalations_per_conversation": round(esc / convs, 2) if convs else None,
            "avg_input_tokens_per_call_mock_estimate": round(tok_in, 0) if tok_in else None}


def model_cost(price, calls, w, fallback=None):
    """USD for `calls` classification calls at `price`.  With cache_hit_rate 0 the prefix is billed as plain
    input (no marker engages below the minimum cacheable size, so no write premium either)."""
    if not w["cache_hit_rate"]:
        return (calls * w["input_tokens_per_call"] * price["input"] + calls * w["output_tokens_per_call"] * price["output"]) / 1e6
    uncached_in = w["input_tokens_per_call"] - w["cached_prefix_tokens"]
    hits = calls * w["cache_hit_rate"]
    misses = calls - hits
    usd = (calls * uncached_in * price["input"]
           + hits * w["cached_prefix_tokens"] * price["cache_read"]
           + misses * w["cached_prefix_tokens"] * price["cache_write_5m"]
           + calls * w["output_tokens_per_call"] * price["output"]) / 1e6
    return usd


def compute(patients, w=WORKLOAD):
    m = PRICES["models"]
    sms = PRICES["sms"]
    inbound = patients * w["inbound_msgs_per_patient"]
    calls = inbound * w["model_calls_per_inbound"]
    out_segments = patients * w["outbound_msgs_per_patient"] * w["segments_per_outbound"]
    in_segments = inbound * w["segments_per_inbound"]
    sms_usd = (out_segments * (sms["twilio_us_outbound_per_segment"] + sms["carrier_fee_per_outbound_segment_assumed"])
               + in_segments * sms["twilio_us_inbound_per_segment"]
               + max(1, patients // 5000) * sms["long_code_number_per_month"])
    hosting_usd = PRICES["hosting_app"]["small_vm_and_managed_db_per_month"]
    esc = patients * w["kate_escalations_per_patient"]
    human_minutes = esc * w["minutes_per_escalation"] + w["oversight_minutes_per_day"] * w["days_per_month"]
    human_hours = human_minutes / 60.0
    human_usd_if_staffed = human_hours * PRICES["human"]["loaded_hourly_rate_nonfounder"]
    clinician_hours = patients * w["clinician_escalations_per_patient"] * 10 / 60.0   # [A] 10 min each, partner's cost

    # A. Opus 5 for everything (uncached baseline)
    a_model = model_cost(m["claude-opus-5"], calls, w)
    # A-cached sensitivity: what A would cost IF the prefix were padded above the cache minimum and 70% of calls hit
    a_cached = model_cost(m["claude-opus-5"], calls, dict(w, cached_prefix_tokens=600, input_tokens_per_call=w["input_tokens_per_call"] + 250, cache_hit_rate=0.7))
    # A'. Sonnet 5 reference
    a2_model = model_cost(m["claude-sonnet-5"], calls, w)
    # B. Haiku first, Opus fallback
    b_model = model_cost(m["claude-haiku-4-5"], calls, w) + model_cost(m["claude-opus-5"], calls * w["fallback_rate_option_b"], w)
    # C. Self-hosted DeepSeek-V4-Flash: 2x H200 (or 4x H100) 24/7 + MLOps fraction; tokens are free at the margin
    cand = PRICES["open_weight_candidate"]
    gpu_opts = []
    for opt in cand["min_config_options"]:
        sku = PRICES["gpu_hosting"][opt["sku"]]
        gpu_opts.append((opt["gpus"], opt["sku"], opt["gpus"] * sku["usd_per_gpu_hour"] * w["gpu_hours_per_month"]))
    gpu_month = min(o[2] for o in gpu_opts)
    gpu_choice = min(gpu_opts, key=lambda o: o[2])
    mlops_usd = w["mlops_fte_fraction_option_c"] * w["engineer_month_loaded"]
    total_tokens = calls * (w["input_tokens_per_call"] + w["output_tokens_per_call"])
    # utilization: tokens/month vs a deliberately generous 500 tok/s sustained capacity figure [A, not benchmarked]
    capacity_tokens = 500 * 3600 * w["gpu_hours_per_month"]
    utilization = total_tokens / capacity_tokens

    def pack(model_usd, extra_usd=0.0, note=""):
        recurring = model_usd + sms_usd + hosting_usd + extra_usd
        return {"model_usd": round(model_usd, 2), "sms_usd": round(sms_usd, 2), "hosting_usd": round(hosting_usd, 2),
                "extra_usd": round(extra_usd, 2), "recurring_usd_excl_human": round(recurring, 2),
                "human_hours": round(human_hours, 1), "human_usd_if_staffed": round(human_usd_if_staffed, 2),
                "recurring_usd_incl_staffed_human": round(recurring + human_usd_if_staffed, 2),
                "per_patient_usd_excl_human": round(recurring / patients, 3),
                "per_patient_usd_incl_staffed_human": round((recurring + human_usd_if_staffed) / patients, 3),
                "note": note}

    return {
        "patients": patients, "model_calls": round(calls), "outbound_segments": round(out_segments),
        "inbound_segments": round(in_segments), "kate_escalations": round(esc), "clinician_hours_partner": round(clinician_hours, 1),
        "A_opus5": pack(a_model, note="Claude Opus 5 every call; no cache discount (prefix below minimum cacheable size)"),
        "A_opus5_cached_sensitivity": pack(a_cached, note="SENSITIVITY: prefix padded to 600 tokens (+250 input/call), 70% cache hits [A] — not the current adapter's behavior"),
        "A2_sonnet5_reference": pack(a2_model, note="Claude Sonnet 5 every call (reference; same architecture)"),
        "B_haiku_then_opus": pack(b_model, note="Claude Haiku 4.5 first, %d%% re-run on Opus 5 [A]" % round(100 * w["fallback_rate_option_b"])),
        "C_selfhost_deepseek_v4_flash": pack(0.0, gpu_month + mlops_usd,
                                             note="%dx %s at $%.2f/GPU-hr 24/7 = $%.0f + MLOps %.2f FTE $%.0f; GPU utilization ≈ %.2f%% [A]"
                                                  % (gpu_choice[0], gpu_choice[1], PRICES["gpu_hosting"][gpu_choice[1]]["usd_per_gpu_hour"],
                                                     gpu_month, w["mlops_fte_fraction_option_c"], mlops_usd, 100 * utilization)),
        "C_gpu_options": gpu_opts,
    }


def markdown(results, measured):
    lines = ["# Monthly recurring cost — order-completion service (synthetic workload assumptions)", "",
             "*Generated by `costs/cost_calculator.py` on %s.  Prices from `costs/prices.json` (each with source + read date).  "
             "Workload rows marked [A] are assumptions; [demo] rows are ratios measured in the synthetic replay and are not evidence of real patient behavior.*" % PRICES["checked_on"], "",
             "## Workload assumptions per enrolled patient per month", "", "| Input | Value | Basis |", "|---|---:|---|"]
    basis = {"outbound_msgs_per_patient": "[A]", "inbound_msgs_per_patient": "[A]", "segments_per_outbound": "[demo]",
             "segments_per_inbound": "[demo]", "model_calls_per_inbound": "[rule]", "input_tokens_per_call": "[A]",
             "cached_prefix_tokens": "[A]", "output_tokens_per_call": "[A]", "cache_hit_rate": "[A]",
             "fallback_rate_option_b": "[A]", "kate_escalations_per_patient": "[A]", "minutes_per_escalation": "[A]",
             "clinician_escalations_per_patient": "[A]", "oversight_minutes_per_day": "[A]", "mlops_fte_fraction_option_c": "[A]"}
    for k, v in WORKLOAD.items():
        if k in basis:
            lines.append("| %s | %s | %s |" % (k, v, basis[k]))
    if measured:
        lines += ["", "Measured in the last synthetic demo run (`var/ocp.sqlite`): " +
                  ", ".join("%s=%s" % (k, v) for k, v in measured.items())]
    lines += ["", "## Results", ""]
    for r in results:
        lines += ["### %s enrolled patients — %s model calls, %s outbound segments, %s Kate escalations, %.1f partner-clinician hours" % (
            "{:,}".format(r["patients"]), "{:,}".format(r["model_calls"]), "{:,}".format(r["outbound_segments"]),
            r["kate_escalations"], r["clinician_hours_partner"]), "",
            "| Option | Model $ | SMS $ | Hosting $ | GPU+MLOps $ | Recurring $ (excl. human) | Human hrs | Human $ if staffed @$%d/hr | Total $ | $/patient (excl. human) | $/patient (incl. staffed human) |" % PRICES["human"]["loaded_hourly_rate_nonfounder"],
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for key, label in (("A_opus5", "A. Opus 5 hosted API (uncached)"), ("A_opus5_cached_sensitivity", "A-cache. Opus 5 if caching engaged (sensitivity)"),
                           ("A2_sonnet5_reference", "A′. Sonnet 5 (reference)"),
                           ("B_haiku_then_opus", "B. Haiku 4.5 → Opus 5 fallback"), ("C_selfhost_deepseek_v4_flash", "C. Self-host DeepSeek-V4-Flash")):
            o = r[key]
            lines.append("| %s | %.2f | %.2f | %.2f | %.2f | %.2f | %.1f | %.2f | %.2f | %.3f | %.3f |" % (
                label, o["model_usd"], o["sms_usd"], o["hosting_usd"], o["extra_usd"], o["recurring_usd_excl_human"],
                o["human_hours"], o["human_usd_if_staffed"], o["recurring_usd_incl_staffed_human"],
                o["per_patient_usd_excl_human"], o["per_patient_usd_incl_staffed_human"]))
        lines += ["", "C note: %s" % r["C_selfhost_deepseek_v4_flash"]["note"], ""]
    d = PRICES["development_one_time"]
    lines += ["## One-time development to reach a live partner (separate from recurring)", "", "| Item | Estimate | Note |", "|---|---:|---|"]
    total_dev = 0.0
    for k, v in d["items"].items():
        usd = v.get("usd") or v["engineer_months"] * d["engineer_month_loaded"]
        total_dev += usd
        lines.append("| %s | $%s | %s |" % (k, "{:,.0f}".format(usd), v["note"]))
    lines += ["| **Total** | **$%s** | engineer month loaded at $%s [A] |" % ("{:,.0f}".format(total_dev), "{:,}".format(d["engineer_month_loaded"])), "",
              "## Unknowns and non-assertions", "",
              "- A2P 10DLC registration and campaign fees: not listed on Twilio's pricing page; unknown.",
              "- Carrier pass-through fees: AT&T/T-Mobile read; Verizon and inbound fees not read; $0.004 blended is an assumption.",
              "- Live model accuracy for intent classification: not measured (mock adapter only).  No benchmark is asserted for any model, including DeepSeek-V4-Flash.",
              "- Open-weight memory footprint (~170 GB) is derived from parameter count and precision, not measured; KV cache and engine overhead are extra.  The 2xH200 / 4xH100 configurations are a chosen serving scenario, not a demonstrated minimum.",
              "- Always-on GPU service (24/7) is a modeling choice driven by patients texting at any hour; a queue-and-batch design could reduce it and was not modeled.  The 500 tokens/s capacity figure behind the utilization estimate is an assumption, not a benchmark.",
              "- BAA availability for RunPod/Lambda GPU rentals: not checked.  Hyperscaler GPU pricing was not read; no multiplier is asserted.",
              "- Twilio lists a failed-message fee and inbound carrier fees on the cited page; neither was read into the model.",
              "- Prompt caching: the live adapter places a cache marker on a ~350-token system prefix, which is below every current model's minimum cacheable size, so the baseline is modeled uncached; the A-cache row shows what padding the prefix would buy.",
              "- Real reply rates, escalation rates, and minutes per escalation: unknown until a partner pilot; the demo fixtures were chosen to exercise failure paths, so they over-state escalations.",
              "- Self-hosting is not cheaper at any of the three scales under these assumptions; it only becomes competitive when monthly token volume approaches the fixed GPU cost, which is far beyond 10,000 patients at this workload.",
              ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--scales", default="100,1000,10000")
    args = ap.parse_args()
    scales = [int(x) for x in args.scales.split(",")]
    results = [compute(n) for n in scales]
    measured = demo_measured()
    if args.json:
        print(json.dumps({"workload": WORKLOAD, "measured_demo": measured, "results": results}, indent=2))
        return 0
    md = markdown(results, measured)
    print(md)
    with open(os.path.join(HERE, "results.md"), "w") as f:
        f.write(md)
    print("(written to costs/results.md)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
