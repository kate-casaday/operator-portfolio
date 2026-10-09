from .base import ModelAdapter, ModelResult, ProviderError  # noqa: F401
from .mock import MockAdapter  # noqa: F401
from .router import RoutedAdapter  # noqa: F401


def build_adapter(name: str = "mock", **kw):
    """Factory.  'mock' (default, credential-free) | 'anthropic' | 'routed'."""
    if name == "mock":
        return MockAdapter(**kw)
    if name == "anthropic":
        from .anthropic_adapter import AnthropicAdapter
        return AnthropicAdapter(**kw)
    if name == "routed":
        from .anthropic_adapter import AnthropicAdapter
        common = {k: v for k, v in kw.items() if k in ("effort", "max_tokens", "max_retries")}
        cheap = AnthropicAdapter(model=kw.get("cheap_model", "claude-haiku-4-5"), **common)
        strong = AnthropicAdapter(model=kw.get("strong_model", "claude-opus-5"), **common)
        return RoutedAdapter(primary=cheap, fallback=strong)
    raise ValueError("unknown adapter %r" % name)
