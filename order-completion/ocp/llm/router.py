"""Selective routing with a shared, bounded attempt budget.

primary (cheap) → if ProviderError or confidence below floor → fallback (strong).
The caller passes `budget` = total provider attempts allowed for this invocation; the router
never exceeds it and reports every attempt in `last_attempts` (including discarded low-confidence
primary results) so instrumentation counts real work, not just the final answer.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .base import ModelAdapter, ModelResult, ProviderError


class RoutedAdapter(ModelAdapter):
    name = "routed"

    def __init__(self, primary: ModelAdapter, fallback: Optional[ModelAdapter] = None,
                 confidence_floor: float = 0.6, retries: int = 1):
        self.primary = primary
        self.fallback = fallback
        self.confidence_floor = confidence_floor
        self.retries = retries
        self.route_log: List = []      # (stage, outcome)
        self.last_attempts: List[Dict] = []
        self.simulated = getattr(primary, "simulated", True) and getattr(fallback, "simulated", True)

    def _attempt(self, adapter: ModelAdapter, context: Dict, text: str, budget: int) -> ModelResult:
        last: Optional[Exception] = None
        for _ in range(min(self.retries + 1, budget)):
            try:
                r = adapter.classify(context, text, budget=1)
                self.last_attempts.extend(adapter.last_attempts)
                return r
            except ProviderError as e:
                self.last_attempts.extend(adapter.last_attempts)
                last = e
        raise ProviderError("%s failed after %d attempts: %s" % (adapter.name, min(self.retries + 1, budget), last))

    def classify(self, context: Dict, patient_text: str, budget: int = 2) -> ModelResult:
        self.last_attempts = []
        spent = 0
        try:
            r = self._attempt(self.primary, context, patient_text, budget)
            spent = len(self.last_attempts)
            if r.confidence >= self.confidence_floor or self.fallback is None:
                self.route_log.append(("primary", "ok"))
                r.adapter = "routed:" + r.adapter
                return r
            self.route_log.append(("primary", "low_confidence"))
            self.last_attempts[-1]["outcome"] = "discarded_low_confidence"
        except ProviderError:
            spent = len(self.last_attempts)
            self.route_log.append(("primary", "error"))
            if self.fallback is None or budget - spent <= 0:
                raise
        if budget - spent <= 0:
            raise ProviderError("attempt budget exhausted before fallback")
        r = self._attempt(self.fallback, context, patient_text, budget - spent)
        r.outcome = "fallback"
        self.last_attempts[-1]["outcome"] = "fallback"
        r.adapter = "routed:" + r.adapter
        self.route_log.append(("fallback", "ok"))
        return r
