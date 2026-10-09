#!/usr/bin/env python3
"""Pressure-test the economics with editable low / medium / high conversational-workload scenarios.

    python3 costs/workload_scenarios.py            # prints markdown, writes costs/workload_results.md
    python3 costs/workload_scenarios.py --json

Separates: patient-service inference · SMS + app infrastructure · Kate's operational time · partner clinician
time · development-agent costs · one-time integration/launch work · founder compensation.  Every number is an
assumption unless marked [demo].  Prices come from costs/prices.json (source + read date per entry).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PRICES = json.load(open(os.path.join(HERE, "prices.json")))
SCEN = json.load(open(os.path.join(HERE, "workload_scenarios.json")))


def model_usd(price, calls, tin, tout):
    return (calls * tin * price["input"] + calls * tout * price["output"]) / 1e6


def scenario_costs(name, sc, patients):
    m = PRICES["models"]
    sms = PRICES["sms"]
    inbound = patients * sc["inbound_msgs"]
    calls = inbound * sc["model_calls_per_inbound"] * sc["retry_and_routing_multiplier"]
    tin, tout = sc["input_tokens_per_call"], sc["output_tokens_per_call"]
    out_seg = patients * sc["outbound_msgs"] * sc["segments_per_outbound"]
    in_seg = inbound * sc["segments_per_inbound"]
    sms_usd = (out_seg * (sms["twilio_us_outbound_per_segment"] + sms["carrier_fee_per_outbound_segment_assumed"])
               + in_seg * sms["twilio_us_inbound_per_segment"] + max(1, patients // 5000) * sms["long_code_number_per_month"])
    infra_usd = PRICES["hosting_app"]["small_vm_and_managed_db_per_month"]
    inference = {
        "A_opus5": model_usd(m["claude-opus-5"], calls, tin, tout),
        "A2_sonnet5": model_usd(m["claude-sonnet-5"], calls, tin, tout),
        "B_haiku_then_opus": model_usd(m["claude-haiku-4-5"], calls, tin, tout) + model_usd(m["claude-opus-5"], calls * 0.25, tin, tout),
        "C_selfhost_deepseek_scenario": SCEN["self_hosted_scenario"]["gpu_usd_per_month"] + SCEN["self_hosted_scenario"]["mlops_usd_per_month"],
    }
    kate_esc = patients * sc["kate_escalations_per_patient"]
    kate_minutes = kate_esc * sc["kate_minutes_per_escalation"] + SCEN["fixed_operational"]["kate_oversight_minutes_per_day"] * SCEN["fixed_operational"]["days_per_month"]
    clin_esc = patients * sc["clinician_escalations_per_patient"]
    clin_minutes = clin_esc * sc["clinician_minutes_per_escalation"]
    unresolved = patients * sc["unresolved_rate"]
    automated = patients - kate_esc - clin_esc          # conversations that never reached a human queue (NOT success)
    return {
        "scenario": name, "patients": patients, "model_calls": round(calls), "tokens_in_M": round(calls * tin / 1e6, 3),
        "tokens_out_M": round(calls * tout / 1e6, 3), "outbound_segments": round(out_seg), "inbound_segments": round(in_seg),
        "inference_usd": {k: round(v, 2) for k, v in inference.items()},
        "sms_usd": round(sms_usd, 2), "infra_usd": round(infra_usd, 2),
        "kate_escalations": round(kate_esc), "kate_hours": round(kate_minutes / 60, 1),
        "clinician_escalations": round(clin_esc), "clinician_hours_partner": round(clin_minutes / 60, 1),
        "unresolved_patients": round(unresolved),
        "no_human_queue_rate": round(max(0.0, automated / patients), 3),
        "unresolved_rate": sc["unresolved_rate"],
        "dev_agent_usd": SCEN["development_agent_costs"]["monthly_usd_assumed"],
    }


def hours_budget_table(sc_name, sc, patients):
    """Max Kate-queue escalation rate that fits each monthly operating-hours budget, at this scale."""
    fixed = SCEN["fixed_operational"]["kate_oversight_minutes_per_day"] * SCEN["fixed_operational"]["days_per_month"] / 60.0
    rows = []
    for h in SCEN["operating_hours_budgets_per_month"]:
        avail = max(0.0, h - fixed)
        max_esc = avail * 60.0 / sc["kate_minutes_per_escalation"]
        rows.append((h, round(fixed, 1), round(max_esc), round(min(1.0, max_esc / patients), 3) if patients else None))
    return rows


SETUP_REASSESSMENT = [
    # task, status, estimate basis, dependency / who quotes
    ("Partner feed adapter + identifier matching (orders, results, cancellations, consent)", "REMAINING — partner-dependent", "1.5 engineer-months [A]", "Partner interface (SFTP/CSV vs FHIR) decides the real size; no quote possible before that"),
    ("Workflow engine, rules, state machines, outbox, holds, recovery", "DONE in synthetic form (this prototype)", "$0 remaining for v1 scope; hardening is ongoing", "Not a production integration; see 'still a hypothesis' list"),
    ("Live model adapter + labeled evaluation set + accuracy/escalation measurement", "PREPARED, not exercised (eval/run_eval.py; no paid calls made)", "1.0 engineer-month [A]", "Partner-approved sample replies; Kate's go-ahead on API spend"),
    ("Messaging go-live: 10DLC brand/campaign, webhook via Twilio SDK validator, delivery receipts, opt-out export", "REMAINING", "0.5 engineer-month [A]; 10DLC fees UNKNOWN (not on Twilio's page)", "Twilio account; partner sender identity"),
    ("Hosting on a BAA-capable cloud; secrets vault; audit retention; dashboard authentication", "REMAINING", "0.5 engineer-month [A] + hosting", "Cloud BAA; not priced here"),
    ("Security design review, testing, remediation", "REMAINING — needs outside quote", "$40,500 allowance [A]; NOT a quote", "Independent security reviewer; quote needed"),
    ("Legal: BAA, partner contact-process approval, consent language, template review", "REMAINING — needs outside quote", "$15,000 allowance [A]", "Counsel; partner compliance"),
    ("Clinician queue integration (how the partner's clinicians receive and close items)", "REMAINING — partner-dependent; NOT in the original $115K", "unknown; 0.25-1.0 engineer-month depending on partner tooling", "Partner clinical operations"),
    ("Identity verification step before texting order detail", "REMAINING — decision needed; NOT in the original $115K", "0.25 engineer-month [A] if DOB challenge", "Kate + partner policy"),
]


def markdown(results, budgets):
    L = ["# Workload scenarios — pressure test of the economics", "",
         "*Generated by `costs/workload_scenarios.py`.  Prices: `costs/prices.json` (source + read date per entry; re-verified 2026-09-15 for Anthropic, RunPod, Twilio, DeepSeek — see that file).  Workload values: `costs/workload_scenarios.json`, all [A] assumptions except where marked [demo].  The baseline row is the round-one short-classification assumption set and does not price a richer longitudinal conversation; low/medium/high do.*", "",
         "## Per scenario and scale (USD per month)", "",
         "| Scenario | Patients | Model calls | Tok in (M) | Tok out (M) | Inference A (Opus 5) | Inference B (Haiku→Opus) | Inference C (self-host scenario) | SMS | Infra | Kate hrs | Clinician hrs (partner) | Unresolved pts | No-human-queue rate |",
         "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in results:
        L.append("| %s | %s | %s | %.3f | %.3f | %.2f | %.2f | %.0f | %.2f | %.0f | %.1f | %.1f | %s | %.0f%% |" % (
            r["scenario"], "{:,}".format(r["patients"]), "{:,}".format(r["model_calls"]), r["tokens_in_M"], r["tokens_out_M"],
            r["inference_usd"]["A_opus5"], r["inference_usd"]["B_haiku_then_opus"], r["inference_usd"]["C_selfhost_deepseek_scenario"],
            r["sms_usd"], r["infra_usd"], r["kate_hours"], r["clinician_hours_partner"], "{:,}".format(r["unresolved_patients"]),
            100 * r["no_human_queue_rate"]))
    L += ["", "**Reading the last two columns together:** 'no-human-queue rate' is the share of patients whose conversation never reached Kate's or the clinician's queue.  It is not a success rate — the unresolved column (patients who never completed, by assumption) sits next to it so that silence cannot be counted as automation working.  Verified completion rate is left null in every scenario because nothing here measures it.", "",
          "## Cost categories, separated (medium scenario shown; others in the JSON output)", ""]
    med = [r for r in results if r["scenario"] == "medium"]
    L += ["| Category | 100 | 1,000 | 10,000 | Basis |", "|---|---:|---:|---:|---|"]
    def rowf(label, key_fn, basis):
        L.append("| %s | %s | %s | %s | %s |" % (label, *["%.0f" % key_fn(r) for r in med], basis))
    rowf("Patient-service inference (A, Opus 5)", lambda r: r["inference_usd"]["A_opus5"], "vendor price × assumed tokens")
    rowf("SMS", lambda r: r["sms_usd"], "Twilio + assumed carrier fees × assumed segments")
    rowf("Application infrastructure", lambda r: r["infra_usd"], "[A] $120/mo small VM + managed DB; BAA host not priced")
    rowf("Kate operational time (hours, not $)", lambda r: r["kate_hours"], "escalations × minutes + 20 min/day oversight [A]")
    rowf("Partner clinician time (hours, partner's cost)", lambda r: r["clinician_hours_partner"], "[A]")
    rowf("Development-agent costs (coding assistants)", lambda r: r["dev_agent_usd"], "[A] $400/mo placeholder; unmetered; UNKNOWN")
    L += ["", "One-time integration and launch work is in the reassessment table below, not in any monthly row.", "",
          "## Which escalation rates fit an operating-hours budget", "",
          "Kate's queue only (partner clinician time is the partner's).  Fixed oversight is subtracted first.  Appropriate clinical escalation is *required*, not a cost to minimize; this table only says what Kate's own queue can absorb.", ""]
    for name, rows_ in budgets:
        L += ["**%s scenario (minutes per escalation as configured)**" % name, "", "| Hours/month budget | Fixed oversight hrs | Max Kate escalations/month | Max rate @100 | @1,000 | @10,000 |", "|---:|---:|---:|---:|---:|---:|"]
        by_h = {}
        for patients, rows in rows_:
            for h, fixed, max_esc, rate in rows:
                by_h.setdefault(h, [fixed, max_esc, {}])[2][patients] = rate
        for h in sorted(by_h):
            fixed, max_esc, rates = by_h[h]
            L.append("| %d | %.1f | %s | %s | %s | %s |" % (h, fixed, "{:,}".format(max_esc),
                     *["%.0f%%" % (100 * rates[p]) if rates.get(p) is not None else "-" for p in (100, 1000, 10000)]))
        L.append("")
    L += ["## Self-hosted configuration — one scenario, labeled", "", SCEN["self_hosted_scenario"]["note"], "",
          "## Reassessment of the ~$115K one-time setup estimate", "",
          "Inherited from round one; the engineer-month rate ($20K loaded) and the security/legal allowances are planning assumptions, not quotes.  A synthetic implementation is not a completed production integration.", "",
          "| Task | Status | Estimate basis | Dependency / who quotes |", "|---|---|---|---|"]
    for t in SETUP_REASSESSMENT:
        L.append("| %s | %s | %s | %s |" % t)
    L += ["", "**Net:** the original five lines ($30K feed adapter + $10K messaging + $20K live-model evaluation + $40.5K security allowance + $15K legal allowance = $115.5K) still stand as allowances.  This table adds one priced line the original omitted (hosting / secrets / dashboard authentication, 0.5 engineer-month, $10K), so the priced remaining work is **$125.5K**, plus two unpriced items (clinician-queue integration; identity verification) and recurring hosting.  The engine/rules line is done in synthetic form and carries no allowance.  No new precision is claimed: two lines need outside quotes and two are partner-dependent before any number is real.  The 'no-human-queue rate' assumes Kate-queue and clinician-queue cohorts do not overlap; label that if the percentage is used outwardly.", ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    results, budgets = [], []
    for name, sc in SCEN["scenarios"].items():
        for n in SCEN["scales"]:
            results.append(scenario_costs(name, sc, n))
        budgets.append((name, [(n, hours_budget_table(name, sc, n)) for n in SCEN["scales"]]))
    if args.json:
        print(json.dumps({"results": results, "setup_reassessment": SETUP_REASSESSMENT}, indent=2))
        return 0
    md = markdown(results, budgets)
    print(md)
    with open(os.path.join(HERE, "workload_results.md"), "w") as f:
        f.write(md)
    print("(written to costs/workload_results.md)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
