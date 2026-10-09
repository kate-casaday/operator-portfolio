#!/usr/bin/env python3
"""Generate the first text for every eligible synthetic patient with the chosen writer, for partner review.

    .venv/bin/python eval/opener_samples.py --composer fact
    .venv/bin/python eval/opener_samples.py --composer anthropic --budget-usd 2.00 --yes-i-accept-paid-calls   # PAID

Writes eval/results/<timestamp>-openers-<composer>.md: one opener per patient with its variant (disclosure, sites,
visit date present), segment count, writer, and any fact-check refusal.  This is the 'sample set of generated
openers' a partner approves before launch (see the version 3 brief).
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
from ocp.rules import Policy  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--composer", default="fact", choices=["fact", "anthropic"])
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--budget-usd", type=float, default=1.00)
    ap.add_argument("--disclosure", default="ab", choices=["ab", "none", "short"])
    ap.add_argument("--yes-i-accept-paid-calls", action="store_true")
    args = ap.parse_args()
    live = args.composer == "anthropic"
    if live and not args.yes_i_accept_paid_calls:
        print("refusing: PAID calls need --yes-i-accept-paid-calls"); return 2
    eng = make_engine(policy=Policy(max_spend_usd_per_day=args.budget_usd, opener_disclosure=args.disclosure), tick=False)
    eng.composer = build_composer(args.composer, model=args.model, effort=args.effort) if live else build_composer("fact")
    eng.tick()
    out = rows(eng.conn, "SELECT m.body, m.segments, m.composer, m.decision, c.opener_variant, p.display_name, p.home_town, p.source_patient_id, "
                         "(SELECT GROUP_CONCAT(l.test_name) FROM order_lines l JOIN orders o ON o.id=l.order_id WHERE o.patient_id=p.id) tests "
                         "FROM messages m JOIN conversations c ON c.id=m.conversation_id JOIN patients p ON p.id=c.patient_id "
                         "WHERE m.template_id='outreach_initial' ORDER BY c.id")
    spend = row(eng.conn, "SELECT COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM model_calls WHERE simulated=0 AND purpose='compose'") or {}
    refused = {json.loads(e["detail"]).get("template"): 1 for e in rows(eng.conn, "SELECT detail FROM events WHERE kind='composer_refused'")}
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    label = ("LIVE %s (%s)" % (args.model, args.effort)) if live else "DETERMINISTIC FACT WRITER"
    path = os.path.join(HERE, "results", "%s-openers-%s.md" % (ts, args.composer))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("# First-text sample set - %s\n\nSYNTHETIC patients only.  %d openers · spend $%.4f · %d compose calls · fallbacks to template: %d\n\n"
                % (label, len(out), spend.get("c") or 0, spend.get("n") or 0, sum(1 for o in out if o["composer"] == "template")))
        for o in out:
            v = json.loads(o["opener_variant"] or "{}")
            f.write("### %s (%s, %s) - %s\n*variant: disclosure=%s · sites named=%s (assigned %s) · visit date=%s · writer=%s · %d segments*\n\n> %s\n\n"
                    % (o["display_name"], o["source_patient_id"], o["home_town"], o["tests"], v.get("disclosure_realized", v.get("disclosure")),
                       v.get("sites_named", "?"), v.get("sites"), "yes" if v.get("visit_date_present") else "no", o["composer"], o["segments"], o["body"]))
            d = json.loads(o["decision"] or "{}")
            if d.get("composer_refused"):
                f.write("> *fact-check refused the model's text (%s); approved template sent instead.*\n\n" % "; ".join(d["composer_refused"]))
    print("%d openers written to %s (spend $%.4f, %d calls)" % (len(out), os.path.relpath(path, ROOT), spend.get("c") or 0, spend.get("n") or 0))
    for o in out[:3]:
        print("-", o["body"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
