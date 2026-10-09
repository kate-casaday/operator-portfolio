"""Live Claude adapter.  NOT exercised by the test suite (no credentials in this repo).

Requires `pip install anthropic` and ANTHROPIC_API_KEY (or `ant auth login`).
The engine still validates every output through rules.validate_model_output, so a
model that returns junk cannot move a workflow anywhere the rules forbid.
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict

from .base import ModelAdapter, ModelResult, INTENT_SCHEMA, SYSTEM_PROMPT, ProviderError


class _Never(Exception):
    """Placeholder exception class used when the SDK is not imported (injected fake client)."""


class AnthropicAdapter(ModelAdapter):
    name = "anthropic"
    simulated = False

    def __init__(self, model: str = "claude-opus-5", effort: str = "low", max_tokens: int = 512, client=None, max_retries: int = 2):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.max_retries = max_retries          # SDK transport retries; the eval runner sets 0 so max-calls bounds provider requests
        self._client = client          # tests inject a fake client; no SDK import happens then
        self._errors = None            # (RateLimitError, APIStatusError, APIConnectionError) once the SDK is imported

    def _client_or_raise(self):
        if self._client is None:
            try:
                import anthropic  # type: ignore
            except ImportError as e:
                raise ProviderError("anthropic SDK not installed: pip install anthropic") from e
            if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                    or os.path.exists(os.path.expanduser("~/.config/anthropic"))):
                raise ProviderError("no Anthropic credential found (ANTHROPIC_API_KEY or `ant auth login`)")
            self._client = anthropic.Anthropic(max_retries=self.max_retries)
            self._errors = (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError)
        return self._client

    @staticmethod
    def _usage_result(usage, data=None) -> ModelResult:
        return ModelResult(
            intent=(data or {}).get("intent", "unclear"), confidence=float((data or {}).get("confidence", 0) or 0),
            barrier=(data or {}).get("barrier", "none"), constraints=(data or {}).get("constraints") or {},
            adapter="anthropic", model="?", simulated=False,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0)

    def classify(self, context: Dict, patient_text: str, budget: int = 1) -> ModelResult:
        t0 = time.perf_counter()
        self.last_attempts = []
        client = self._client_or_raise()
        rate_limit, status_err, conn_err = self._errors or (_Never, _Never, _Never)

        def fail(msg, resp=None):
            """Record the attempt (with the response's real usage when a response exists) and raise."""
            if resp is not None and getattr(resp, "usage", None) is not None:
                r = self._usage_result(resp.usage)
                r.model, r.latency_ms = self.model, (time.perf_counter() - t0) * 1000
                self.last_attempts = [self.attempt_record(self.name, self.model, False, "error", r, error=msg)]
            else:
                self.last_attempts = [self.attempt_record(self.name, self.model, False, "error",
                                                          latency_ms=(time.perf_counter() - t0) * 1000, error=msg)]
            raise ProviderError(msg)

        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                # Explicit breakpoint on the stable system prefix.  NOTE: the prefix (~350 tokens) is below the
                # documented minimum cacheable size (512 for Opus 5, 1024 Sonnet 5, 4096 Haiku 4.5), so no cache
                # discount applies until the prompt grows; cost_calculator.py therefore models uncached usage.
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=self.build_messages(context, patient_text),
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": INTENT_SCHEMA}},
            )
        except rate_limit as e:
            fail("rate limited: %s" % e)
        except status_err as e:
            fail("api status %s: %s" % (getattr(e, "status_code", "?"), e))
        except conn_err as e:
            fail("connection error: %s" % e)
        latency = (time.perf_counter() - t0) * 1000
        if getattr(resp, "stop_reason", None) == "refusal":
            fail("model refused (stop_reason=refusal)", resp)
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            fail("non-JSON model output", resp)
        usage = resp.usage
        r = ModelResult(
            intent=data.get("intent", "unclear"), confidence=float(data.get("confidence", 0)),
            barrier=data.get("barrier", "none"), constraints=data.get("constraints") or {},
            adapter=self.name, model=self.model, simulated=False,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            latency_ms=latency, raw=data, proposed_action=data.get("proposed_action"))
        self.last_attempts = [self.attempt_record(self.name, self.model, False, "ok", r)]
        return r
