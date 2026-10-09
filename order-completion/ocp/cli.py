"""Command line entry points.  Run from order-completion:

  python3 -m ocp seed            # fresh synthetic session: 20 interactive patients, initial outreach sent (Kate's start point)
  python3 -m ocp demo            # scripted 20-scenario replay, prints narrative + PASS/FAIL ledger
  python3 -m ocp serve           # dashboard + simulated SMS console on http://127.0.0.1:8765
  python3 -m ocp reset           # delete the local database
  python3 -m ocp metrics         # print the instrumentation roll-up as JSON
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .db import connect
from .directory import Directory
from .engine import Engine
from .llm import build_adapter
from .messaging import build_messaging
from .metrics import summary
from .rules import Policy

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(HERE, "var", "ocp.sqlite")
DEFAULT_DIR = os.path.join(HERE, "data", "partner_directory.json")


def build_engine(db_path: str = DEFAULT_DB, model: str = "mock", messaging: str = "simulated",
                 directory_path: str = DEFAULT_DIR, composer: str = "auto", composer_model: str = "claude-opus-5") -> Engine:
    conn = connect(db_path)
    directory = Directory.load(directory_path)
    from .db import get_setting
    from .llm.composer import build_composer
    policy = Policy()
    t = get_setting(conn, "overdue_threshold_days")
    if t:
        policy.min_order_age_days = int(t)
    for key in ("clinical_handoff", "resolver_mode", "operator_role"):   # version 4/5 settings survive a restart
        v = get_setting(conn, key)
        if v:
            setattr(policy, key, v)
    if composer == "auto":
        composer = "anthropic" if model in ("anthropic", "routed") else "fact"
    comp = build_composer(composer, model=composer_model) if composer == "anthropic" else build_composer(composer)
    # version 4: the resolver follows the model choice (live model → live resolver); the reviewer follows the policy
    from .llm.resolver import build_resolver, build_reviewer
    live = model in ("anthropic", "routed")
    resolver = build_resolver("anthropic", model=composer_model) if live else build_resolver("rules")
    reviewer = build_reviewer(policy.resolver_reviewer) if live else build_reviewer("rules")
    from .feed_integrity import InboxFeedSource
    inbox = InboxFeedSource(os.path.join(os.path.dirname(os.path.abspath(db_path)), "feed_inbox"), settle_s=2.0) if db_path != ":memory:" else None   # Sept 23: drop a partner file here; it is picked up when the tick runs (nothing schedules the tick in this prototype)
    return Engine(conn, directory, build_adapter(model), build_messaging(messaging), policy, composer=comp, resolver=resolver, reviewer=reviewer, feed_source=inbox)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ocp", description="StealthCo order-completion prototype (synthetic only)")
    ap.add_argument("command", choices=["seed", "demo", "serve", "reset", "metrics"])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--model", default="mock", choices=["mock", "anthropic", "routed"],
                    help="mock = deterministic stand-in (default, no credentials); anthropic/routed = live Claude")
    ap.add_argument("--messaging", default="simulated", choices=["simulated", "twilio"])
    ap.add_argument("--composer", default="auto", choices=["auto", "fact", "anthropic"],
                    help="who writes the words: fact = deterministic (default with --model mock); anthropic = Claude (default with a live model)")
    ap.add_argument("--composer-model", default="claude-opus-5")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.command == "reset":
        for suffix in ("", "-journal"):
            try:
                os.remove(args.db + suffix)
            except FileNotFoundError:
                pass
        print("removed", args.db)
        return 0

    if args.command == "seed":
        engine = build_engine(args.db, args.model, args.messaging, composer=args.composer, composer_model=args.composer_model)
        from .scenarios import load_orders_feed, SIM_START
        r = engine.seed_session(load_orders_feed(), SIM_START)
        print("Fresh synthetic session at %s: %s patients, %s eligible, %s interactive conversations (initial outreach sent), "
              "%s feedback rows kept." % (args.db, r["patients"], r["eligible"], r["interactive_conversations"], r["feedback_rows_kept"]))
        print("Now: python3 -m ocp serve  → open http://127.0.0.1:8765 and pick any patient in state outreach_sent.")
        return 0

    if args.command == "demo":
        if os.path.exists(args.db):
            os.remove(args.db)
        engine = build_engine(args.db, args.model, args.messaging, composer=args.composer, composer_model=args.composer_model)
        from .scenarios import run_demo
        print("MODEL ADAPTER: %s  (%s)" % (engine.model.name, "SIMULATED classifier, not a language model"
                                            if engine.model.name == "mock" else "LIVE model calls"))
        print("MESSAGING: %s  (%s)" % (engine.messaging.name, "nothing leaves this process"
                                        if engine.messaging.simulated else "REAL SMS transport"))
        results = run_demo(engine, quiet=args.quiet)
        passed = sum(1 for r in results if r["passed"])
        print("\n=== Scenario ledger: %d/%d passed" % (passed, len(results)))
        for r in results:
            print("  [%s] %2d  %s" % ("PASS" if r["passed"] else "FAIL", r["id"], r["title"]))
        print("\n=== Instrumentation (read back from the database)")
        print(json.dumps(summary(engine.conn), indent=2))
        print("\nDatabase kept at %s — run `python3 -m ocp serve` to open the dashboard on it." % args.db)
        return 0 if passed == len(results) else 1

    if args.command == "metrics":
        engine = build_engine(args.db, args.model, args.messaging, composer=args.composer, composer_model=args.composer_model)
        print(json.dumps(summary(engine.conn), indent=2))
        return 0

    if args.command == "serve":
        from .server import serve
        engine = build_engine(args.db, args.model, args.messaging, composer=args.composer, composer_model=args.composer_model)
        serve(engine, port=args.port)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
