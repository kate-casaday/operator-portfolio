"""StealthCo order-completion prototype (synthetic data only).

Package layout:
  db.py          sqlite schema and connection helpers
  models.py      enums, state machine definitions, dataclasses
  rules.py       explicit software rules: allowed actions, transitions, limits
  directory.py   verified partner sites / hours / links / approved instructions
  templates.py   approved outbound message templates (the model never free-writes to patients)
  importer.py    partner feed import (orders >= 45 days, completion/cancellation updates)
  feed_integrity.py  Sept 23: validated arrival paths, quarantine, held files, per-partner health, alerts to the partner's technical contact
  engine.py      orchestrator: inbound handling, scheduler tick, escalation, verification
  metrics.py     instrumentation (tokens, calls, segments, latency, completions, human minutes)
  llm/           replaceable model adapters (mock = default, anthropic = live, router = selective)
  messaging/     replaceable SMS adapters (simulated = default, twilio = disabled by default)
  server.py      stdlib HTTP dashboard + JSON API + simulated SMS console
  cli.py         command line entry points
"""
__version__ = "0.3.0"
