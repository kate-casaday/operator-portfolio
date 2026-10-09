from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class MessagingError(Exception):
    """The provider definitely did NOT accept the message.  The engine retries up to policy.sms_max_attempts."""


class AmbiguousSendError(Exception):
    """The provider may or may not have accepted the message (timeout, 5xx, unreadable response).
    Deliberately not a MessagingError: the engine marks the row ambiguous and never resends automatically."""


@dataclass
class SendResult:
    provider: str
    provider_message_id: str
    simulated: bool
    status: str = "sent"


class MessagingAdapter:
    name = "base"
    simulated = True

    def send(self, to: str, body: str, dedupe_key: str) -> SendResult:  # pragma: no cover - interface
        raise NotImplementedError
