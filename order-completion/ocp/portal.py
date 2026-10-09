"""Version 4: the partner's patient-portal messaging, as an adapter (like the Twilio adapter: real interface, no real
sending here).

portal_relay mode sends the patient's OWN words to the ordering provider's office as a patient-authored message.
The application supplies the subject (order reference) and the body verbatim; no model writes any of it.
Production portal messaging is partner-dependent.  This prototype does not verify any vendor's write interface or
permissions; each partner needs per-site enablement and its own approval of a third party sending patient-authored
messages.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional


class PortalError(Exception):
    """The portal definitely did not accept the message."""


@dataclass
class PortalResult:
    adapter: str
    provider_message_id: str
    simulated: bool
    status: str = "sent"


class PortalAdapter:
    name = "base"
    simulated = True

    def send_patient_message(self, patient_ref: str, provider: str, subject: str, body: str, order_refs: List[str]) -> PortalResult:  # pragma: no cover
        raise NotImplementedError


class SimulatedPortal(PortalAdapter):
    """Records the message in memory; nothing leaves the process."""
    name = "simulated"
    simulated = True

    def __init__(self, fail_times: int = 0):
        self.sent: List[Dict] = []
        self.fail_times = fail_times

    def send_patient_message(self, patient_ref, provider, subject, body, order_refs) -> PortalResult:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise PortalError("simulated portal failure")
        mid = "PORTAL-" + uuid.uuid4().hex[:12]
        self.sent.append({"patient_ref": patient_ref, "provider": provider, "subject": subject, "body": body, "order_refs": list(order_refs), "id": mid})
        return PortalResult(adapter=self.name, provider_message_id=mid, simulated=True)


def build_portal(name: str = "simulated", **kw) -> PortalAdapter:
    if name == "simulated":
        return SimulatedPortal(**kw)
    raise ValueError("unknown portal adapter %r (production adapters are partner-specific and not in the prototype)" % name)
