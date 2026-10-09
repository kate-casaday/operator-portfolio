"""In-memory SMS transport.  Nothing leaves the process.

IDs are UUIDs so they stay unique across process restarts against the same database.
`crash_after_accept` simulates the worst case: the provider accepted the message but the
process died before recording it (used by the restart/ambiguity tests)."""
from __future__ import annotations

import uuid
from typing import Dict, List

from .base import MessagingAdapter, SendResult, MessagingError


class SimulatedCrash(RuntimeError):
    """Not a MessagingError on purpose: the engine must treat it as 'unknown outcome', not 'retry'."""


class SimulatedMessaging(MessagingAdapter):
    name = "simulated"
    simulated = True

    def __init__(self, fail_times: int = 0, crash_after_accept: int = 0):
        self.sent: List[Dict] = []
        self.fail_times = fail_times
        self.crash_after_accept = crash_after_accept

    def send(self, to: str, body: str, dedupe_key: str) -> SendResult:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise MessagingError("simulated carrier failure")
        mid = "SIM-" + uuid.uuid4().hex[:12]
        self.sent.append({"to": to, "body": body, "dedupe_key": dedupe_key, "id": mid})
        if self.crash_after_accept > 0:
            self.crash_after_accept -= 1
            raise SimulatedCrash("process died after provider accepted %s" % mid)
        return SendResult(provider=self.name, provider_message_id=mid, simulated=True)
