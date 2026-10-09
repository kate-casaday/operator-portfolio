import os
import unittest

from ocp.messaging.base import MessagingError
from ocp.messaging.twilio_adapter import TwilioMessaging


class TwilioTests(unittest.TestCase):
    def setUp(self):
        for k in ("OCP_TWILIO_SEND_ENABLED", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
            os.environ.pop(k, None)

    def test_sending_disabled_by_default(self):
        t = TwilioMessaging()
        self.assertFalse(t.send_enabled); self.assertFalse(t.ready)
        with self.assertRaises(MessagingError):
            t.send("+12075550100", "hi", "k")

    def test_enabled_flag_without_credentials_still_refuses(self):
        os.environ["OCP_TWILIO_SEND_ENABLED"] = "1"
        t = TwilioMessaging()
        self.assertTrue(t.send_enabled); self.assertFalse(t.ready)
        with self.assertRaises(MessagingError):
            t.send("+12075550100", "hi", "k")

    def test_signature_verification(self):
        os.environ["TWILIO_AUTH_TOKEN"] = "12345"
        t = TwilioMessaging()
        url = "https://mycompany.com/myapp.php?foo=1&bar=2"
        params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
                  "From": "+12349013030", "To": "+18005551212"}
        sig = TwilioMessaging.compute_signature("12345", url, params)
        # Self-consistency only.  Twilio's public docs (checked 2026-09-15) show an example signature but not
        # the auth token that produced it, so this implementation is NOT verified against a known-good vector.
        # Production must use Twilio's SDK validator, per Twilio's own guidance.
        self.assertEqual(len(sig), 28)
        self.assertTrue(t.verify_signature(url, params, sig))
        self.assertFalse(t.verify_signature(url, params, "nope"))
        self.assertFalse(t.verify_signature(url, dict(params, Digits="9"), sig))

    def test_parse_inbound(self):
        p = TwilioMessaging.parse_inbound({"From": "+1", "To": "+2", "Body": "STOP", "MessageSid": "SM1"})
        self.assertEqual(p, {"from": "+1", "to": "+2", "body": "STOP", "provider_message_id": "SM1"})


if __name__ == "__main__":
    unittest.main()
