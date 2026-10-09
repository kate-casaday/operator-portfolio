"""Service catalog: which collection service an order needs, what the assistant may call it, and whether the
test may ever be named in a text.  Loaded from data/service_catalog.json (partner-approved in production)."""
from __future__ import annotations

import json
import os
from typing import Dict, Iterable, List, Optional, Set

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "service_catalog.json")


class Catalog:
    def __init__(self, data: Dict):
        self.services: Dict[str, Dict] = data.get("services", {})
        self.tests: Dict[str, Dict] = data.get("tests", {})

    @classmethod
    def load(cls, path: str = DEFAULT_PATH) -> "Catalog":
        with open(path) as f:
            return cls(json.load(f))

    UNMAPPED = "unmapped_test_code"

    def requirements(self, test_codes: Iterable[str]) -> Set[str]:
        """Union of services every open line needs.  An unknown code FAILS CLOSED: it adds the sentinel
        `unmapped_test_code`, which no site offers, so nothing is recommended until a person maps the code
        (`unknown_codes` names them)."""
        req: Set[str] = set()
        for code in test_codes:
            t = self.tests.get((code or "").upper())
            req.update(t["requires"] if t else [self.UNMAPPED])
        return req

    def unknown_codes(self, test_codes: Iterable[str]) -> List[str]:
        return sorted({c for c in test_codes if (c or "").upper() not in self.tests})

    def category(self, test_codes: Iterable[str]) -> str:
        """Plain-language category for a first text.  Several categories → the most general phrase."""
        cats = []
        for code in test_codes:
            t = self.tests.get((code or "").upper())
            c = t["category"] if t else "blood work"
            if c not in cats:
                cats.append(c)
        if not cats:
            return "lab testing"
        if len(cats) == 1:
            return cats[0]
        return "lab testing"

    def sensitive(self, test_codes: Iterable[str]) -> bool:
        return any((self.tests.get((c or "").upper()) or {}).get("sensitive") for c in test_codes)

    def nameable(self, test_codes: Iterable[str]) -> List[str]:
        """Test names the assistant may say once the patient has replied (never sensitive ones)."""
        out = []
        for code in test_codes:
            t = self.tests.get((code or "").upper())
            if t and not t.get("sensitive"):
                out.append(t["name"])
        return out

    def timing(self, requirements: Iterable[str]) -> Dict:
        """Timing constraints implied by the services (latest start for a timed test; fasting)."""
        out: Dict = {}
        for r in requirements:
            s = self.services.get(r) or {}
            if s.get("latest_start"):
                out["latest_start"] = min(out.get("latest_start", "23:59"), s["latest_start"])
            if s.get("timed_minutes"):
                out["timed_minutes"] = max(out.get("timed_minutes", 0), s["timed_minutes"])
            if s.get("fasting"):
                out["fasting"] = True
        return out

    def prep_instruction(self, test_codes: Iterable[str], now=None) -> Optional[str]:
        """Partner-approved preparation text for EVERY open line, joined; None if any line lacks approved text
        (then the question is a clinical handoff, not an application answer).  Version 4."""
        parts: List[str] = []
        for code in test_codes:
            t = self.tests.get((code or "").upper())
            pi = (t or {}).get("prep_instruction") or {}
            if not (pi.get("text") and pi.get("approved_at") and pi.get("approved_by")):
                return None
            if now is not None:
                try:
                    from datetime import datetime as _dt
                    if (now - _dt.fromisoformat(pi["approved_at"])).days > 365:
                        return None
                except ValueError:
                    return None
            if pi["text"] not in parts:
                parts.append(pi["text"])
        return " ".join(parts) if parts else None

    def describe(self, requirements: Iterable[str]) -> str:
        return ", ".join(self.services.get(r, {}).get("label", "unmapped test code (needs review)" if r == self.UNMAPPED else r) for r in sorted(requirements))
