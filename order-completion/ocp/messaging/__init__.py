from .base import MessagingAdapter, SendResult, MessagingError  # noqa: F401
from .simulated import SimulatedMessaging  # noqa: F401


def build_messaging(name: str = "simulated", **kw):
    if name == "simulated":
        return SimulatedMessaging(**kw)
    if name == "twilio":
        from .twilio_adapter import TwilioMessaging
        return TwilioMessaging(**kw)
    raise ValueError("unknown messaging adapter %r" % name)
