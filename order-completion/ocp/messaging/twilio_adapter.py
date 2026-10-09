"""Twilio adapter.  Outbound sending is DISABLED unless all three hold:
  OCP_TWILIO_SEND_ENABLED=1, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN (env only, never files).

Uses the REST API over urllib (no SDK dependency).  Inbound webhooks are parsed
and signature-checked per Twilio's HMAC-SHA1 scheme.  No test in this repo sends
anything; the test suite only exercises the disabled path and signature math.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import urllib.parse
import urllib.request
from typing import Dict, Optional

import socket
import urllib.error

from .base import MessagingAdapter, SendResult, MessagingError, AmbiguousSendError


class TwilioMessaging(MessagingAdapter):
    name = "twilio"
    simulated = False

    def __init__(self, from_number: Optional[str] = None, messaging_service_sid: Optional[str] = None):
        self.from_number = from_number or os.environ.get("TWILIO_FROM_NUMBER")
        self.messaging_service_sid = messaging_service_sid or os.environ.get("TWILIO_MESSAGING_SERVICE_SID")
        self.account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        self.auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        self.send_enabled = os.environ.get("OCP_TWILIO_SEND_ENABLED") == "1"

    @property
    def ready(self) -> bool:
        return bool(self.send_enabled and self.account_sid and self.auth_token
                    and (self.from_number or self.messaging_service_sid))

    def send(self, to: str, body: str, dedupe_key: str) -> SendResult:
        if not self.send_enabled:
            raise MessagingError("twilio outbound disabled (set OCP_TWILIO_SEND_ENABLED=1 to enable)")
        if not self.ready:
            raise MessagingError("twilio credentials or sender missing in environment")
        url = "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % self.account_sid
        fields = {"To": to, "Body": body}
        if self.messaging_service_sid:
            fields["MessagingServiceSid"] = self.messaging_service_sid
        else:
            fields["From"] = self.from_number
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        token = base64.b64encode(("%s:%s" % (self.account_sid, self.auth_token)).encode()).decode()
        req.add_header("Authorization", "Basic " + token)
        req.add_header("Idempotency-Key", dedupe_key)   # defensive; Twilio does not guarantee dedupe on this
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # 4xx: Twilio rejected the request → definitely not accepted.  5xx: unknown whether it was created.
            if 400 <= e.code < 500:
                raise MessagingError("twilio rejected the request: HTTP %d" % e.code) from e
            raise AmbiguousSendError("twilio returned HTTP %d; acceptance unknown" % e.code) from e
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", None)
            # Only failures that establish NO request reached Twilio are "definitely not accepted":
            # refused connection, DNS failure.  A reset/broken pipe/anything else may have happened after
            # transmission, so it is treated as unknown outcome (never blindly retried).
            if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
                raise MessagingError("twilio unreachable before send: %s" % reason.__class__.__name__) from e
            raise AmbiguousSendError("twilio request failed with %s; acceptance unknown" % reason.__class__.__name__) from e
        except (socket.timeout, TimeoutError) as e:
            raise AmbiguousSendError("twilio request timed out; acceptance unknown") from e
        except (ValueError, OSError) as e:
            # response arrived but could not be read/parsed: the message may exist
            raise AmbiguousSendError("twilio response unreadable: %s" % e.__class__.__name__) from e
        return SendResult(provider=self.name, provider_message_id=payload.get("sid", ""), simulated=False,
                          status=payload.get("status", "queued"))

    # --- inbound -----------------------------------------------------------------
    @staticmethod
    def compute_signature(auth_token: str, url: str, params: Dict[str, str]) -> str:
        s = url + "".join(k + params[k] for k in sorted(params))
        digest = hmac.new(auth_token.encode(), s.encode(), hashlib.sha1).digest()
        return base64.b64encode(digest).decode()

    def verify_signature(self, url: str, params: Dict[str, str], signature: str) -> bool:
        if not self.auth_token:
            return False
        return hmac.compare_digest(self.compute_signature(self.auth_token, url, params), signature or "")

    @staticmethod
    def parse_inbound(form: Dict[str, str]) -> Dict[str, str]:
        return {"from": form.get("From", ""), "to": form.get("To", ""), "body": form.get("Body", ""),
                "provider_message_id": form.get("MessageSid", "")}
