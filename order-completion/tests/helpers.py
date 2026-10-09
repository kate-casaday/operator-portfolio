"""Shared fixtures for the unittest suite.  Everything in-memory, everything synthetic."""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict, List

from ocp.db import connect
from ocp.directory import Directory
from ocp.engine import Engine
from ocp.llm.base import ModelAdapter, ModelResult
from ocp.llm.mock import MockAdapter
from ocp.messaging.simulated import SimulatedMessaging
from ocp.rules import Policy
from ocp.scenarios import SIM_START, DATA

ORDERS = os.path.join(DATA, "synthetic_orders.json")
DIRECTORY = os.path.join(DATA, "partner_directory.json")


class SpyAdapter(ModelAdapter):
    """Wraps another adapter and records every context it was shown."""
    name = "spy"

    def __init__(self, inner: ModelAdapter):
        self.inner = inner
        self.contexts: List[Dict] = []
        self.texts: List[str] = []

    def classify(self, context, patient_text, budget=1):
        self.contexts.append(json.loads(json.dumps(context)))
        self.texts.append(patient_text)
        r = self.inner.classify(context, patient_text, budget=budget)
        self.last_attempts = self.inner.last_attempts
        return r


class FixedAdapter(ModelAdapter):
    """Returns a scripted result regardless of input (for low-confidence / junk-output tests)."""
    name = "fixed"

    def __init__(self, **kw):
        self.kw = kw

    def classify(self, context, patient_text, budget=1):
        r = ModelResult(adapter="fixed", model="fixed", simulated=True, **self.kw)
        self.last_attempts = [self.attempt_record("fixed", "fixed", True, "ok", r)]
        return r


# Version 4 changed three defaults (clinical handoff → the partner's portal link, the operational resolver on, a numbered
# menu after two unclear replies).  Tests written against version 3's routing pin these so they keep testing what they were
# written to test; tests/test_v4.py exercises the version 4 defaults explicitly.
V3_ROUTING = dict(clinical_handoff="clinician_queue", resolver_mode="off", unclear_menu=False, booking_enabled=False)


# Version 5 offers simulated booking at bookable sites, so a confirmed day now yields `offer_slots` before `plan_confirmed`.
# Tests about walk-in plan mechanics pin booking off.
def walkin_policy(**kw) -> Policy:
    return Policy(**{"booking_enabled": False, **kw})


def v3_policy(**kw) -> Policy:
    return Policy(**{**V3_ROUTING, **kw})


def make_engine(model=None, messaging=None, policy=None, import_feed=True, tick=True, db_path=":memory:"):
    conn = connect(db_path)
    directory = Directory.load(DIRECTORY)
    eng = Engine(conn, directory, model or MockAdapter(), messaging or SimulatedMessaging(), policy or Policy())
    eng.set_now(SIM_START)
    if import_feed:
        with open(ORDERS) as f:
            eng.import_orders(json.load(f), source_name="test")
        eng.import_updates(feed([]), source_name="test")
        if tick:
            eng.tick()
    return eng


def patient(eng, n: int):
    from ocp.db import row
    return row(eng.conn, "SELECT * FROM patients WHERE source_patient_id=?", ("P-%02d" % n,))


def conv(eng, n: int):
    from ocp.db import row
    return row(eng.conn, "SELECT * FROM conversations WHERE patient_id=?", (patient(eng, n)["id"],))


def orders(eng, n: int):
    from ocp.db import rows
    return rows(eng.conn, "SELECT * FROM orders WHERE patient_id=? ORDER BY id", (patient(eng, n)["id"],))


def msgs(eng, n: int, direction=None):
    from ocp.db import rows
    c = conv(eng, n)
    q = "SELECT * FROM messages WHERE conversation_id=?" + (" AND direction=?" if direction else "") + " ORDER BY id"
    return rows(eng.conn, q, (c["id"], direction) if direction else (c["id"],))


def escalations(eng, n: int):
    from ocp.db import rows
    return rows(eng.conn, "SELECT * FROM escalations WHERE conversation_id=? ORDER BY id", (conv(eng, n)["id"],))


def feed(updates, at=None):
    return {"partner_id": "RIVERBEND", "generated_at": (at or SIM_START).isoformat(), "updates": updates}


def refresh(eng):
    """A partner's daily files (orders + updates) with no changes, stamped now."""
    eng.import_orders({"partner_id": "RIVERBEND", "generated_at": eng.now().isoformat(), "orders": []})
    eng.import_updates(feed([], eng.now()))


PHONE = {i: "+120755501%02d" % i for i in range(1, 21)}
