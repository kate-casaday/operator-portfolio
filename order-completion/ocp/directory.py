"""Verified partner directory.  Only entries here may be offered to patients.

Every site carries `verified_at` and `verified_by`; every approved instruction carries `approved_at`.
Validity is checked **at the time of use** (the engine passes its clock), never only at load.
Sites are refused when verification is missing, unsigned, or older than `max_age_days`.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timedelta
from typing import Dict, Iterable, List, Optional

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Directory:
    def __init__(self, data: Dict, max_age_days: int = 90):
        self.partner_id = data["partner_id"]
        self.partner_name = data["partner_name"]
        self.clinician_contact = data.get("clinician_contact", {})
        self.technical_contact = data.get("technical_contact", {}) or {}   # Sept 23: who hears about feed problems (we tell them; they do not watch for us)
        self._instructions = data.get("approved_instructions", {})
        self._sites: List[Dict] = list(data.get("sites", []))
        self.towns: Dict[str, Dict] = data.get("towns", {})
        self.mobile_routes: List[Dict] = data.get("mobile_routes", [])
        self.patient_portal: Dict = data.get("patient_portal", {}) or {}
        self.urgent_care: List[Dict] = data.get("urgent_care", []) or []
        # version 5: partner policy records (drafts until approved; the dashboard says which)
        self.escalation_routes: Dict = data.get("escalation_routes", {}) or {}
        self.urgency_policy: Dict = data.get("urgency_policy", {}) or {}
        self.escalation_criteria: Dict = data.get("escalation_criteria", {}) or {}
        self.capabilities: Dict = data.get("capabilities", {}) or {}
        self.max_age_days = max_age_days

    def granted(self, name: str) -> bool:
        """Partner-granted operational capability, DEFAULT DENY (V5-1).  `clinician_queue` is granted when its block says
        accepted; every other flag must be literally true."""
        c = self.capabilities or {}
        if name == "clinician_queue":
            v = c.get("clinician_queue")
            return bool(v.get("accepted")) if isinstance(v, dict) else bool(v)
        return c.get(name) is True

    @classmethod
    def load(cls, path: str, now: Optional[datetime] = None) -> "Directory":
        with open(path) as f:
            return cls(json.load(f))

    # ---- validity at time of use --------------------------------------------------
    def _site_valid(self, s: Dict, now: datetime) -> Optional[str]:
        v = s.get("verified_at")
        if not v or not s.get("verified_by"):
            return "no signed verification"
        age = (now - datetime.fromisoformat(v)).days
        if age > self.max_age_days:
            return "verification %d days old" % age
        if not s.get("hours"):
            return "no hours"
        return None

    def sites(self, now: datetime) -> List[Dict]:
        return [s for s in self._sites if self._site_valid(s, now) is None]

    def rejected(self, now: datetime) -> List[str]:
        return ["%s: %s" % (s.get("id"), self._site_valid(s, now)) for s in self._sites if self._site_valid(s, now)]

    def instruction(self, key: str, now: datetime) -> Optional[str]:
        """Approved instruction text, or None if missing/unapproved/expired."""
        ins = self._instructions.get(key)
        if not isinstance(ins, dict) or not ins.get("text") or not ins.get("approved_at") or not ins.get("approved_by"):
            return None
        if (now - datetime.fromisoformat(ins["approved_at"])).days > self.max_age_days:
            return None
        return ins["text"]

    def site_meta(self, site_id: str) -> Optional[str]:
        """Content digest of the site record (facts + verification + signer); any change invalidates queued text."""
        for s in self._sites:
            if s["id"] == site_id:
                return hashlib.sha1(json.dumps(s, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return None

    def instruction_meta(self, key: str) -> Optional[str]:
        """Content digest of the instruction (text + approval + signer)."""
        ins = self._instructions.get(key)
        if not isinstance(ins, dict):
            return None
        return hashlib.sha1(json.dumps(ins, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def site(self, site_id: Optional[str], now: datetime) -> Optional[Dict]:
        for s in self.sites(now):
            if s["id"] == site_id:
                return s
        return None

    # ---- capability -------------------------------------------------------------------
    @staticmethod
    def capable(s: Dict, requirements: Optional[Iterable[str]]) -> bool:
        """A site may be offered only if it offers every service the order needs.  A site with no `services`
        list is treated as blood-draw-only (the conservative reading of an incomplete record)."""
        req = set(requirements or [])
        if not req:
            return True
        return req <= Directory.services_of(s)

    @staticmethod
    def services_of(s: Dict) -> set:
        """A record with no `services` key is read as blood-draw-only; an explicit empty list means it offers nothing."""
        if "services" not in s or s.get("services") is None:
            return {"blood_draw"}
        return set(s.get("services") or [])

    def incapable_reason(self, s: Dict, requirements: Optional[Iterable[str]]) -> Optional[str]:
        missing = set(requirements or []) - self.services_of(s)
        return ("does not offer " + ", ".join(sorted(missing))) if missing else None

    def latest_start(self, s: Dict, requirements: Optional[Iterable[str]], default: Optional[str] = None) -> Optional[str]:
        """Effective 'latest start' for the required services: the site's own service-hour limit when it has one,
        otherwise the catalog default passed by the caller.  One rule for selection AND wording."""
        best = default
        for r in (requirements or []):
            ls = ((s.get("service_hours") or {}).get(r) or {}).get("latest_start")
            if ls and (best is None or ls < best):
                best = ls
        return best

    # ---- selection ------------------------------------------------------------------
    def filter(self, constraints: Dict, now: datetime, town: Optional[str] = None,
               requirements: Optional[Iterable[str]] = None, latest_start: Optional[str] = None) -> List[Dict]:
        """Sites satisfying ALL stated constraints (after_time, before_time, weekday, weekend_ok) AND able to
        perform the required services.  A timed service with a latest start narrows the usable window."""
        out = []
        for s in self.nearest(town, now, limit=None, requirements=requirements):
            latest = self.latest_start(s, requirements, default=latest_start)
            days = list(s["hours"].keys())
            if constraints.get("weekday"):
                days = [d for d in days if d == constraints["weekday"]]
            elif constraints.get("weekend_ok"):
                days = [d for d in days if d in ("sat", "sun")]
            ok_days = []
            for d in days:
                span = s["hours"].get(d)
                if not span:
                    continue
                if constraints.get("after_time") and not _parse(span[1]) > _parse(constraints["after_time"]):
                    continue
                if constraints.get("before_time") and not _parse(span[0]) < _parse(constraints["before_time"]):
                    continue
                if latest and constraints.get("after_time") and not _parse(constraints["after_time"]) < _parse(latest):
                    continue           # patient can only come after the last allowed start of a timed test
                if latest and not _parse(span[0]) < _parse(latest):
                    continue           # site opens after the last allowed start
                ok_days.append(d)
            if ok_days:
                out.append(s)
        return out

    def nearest(self, town: Optional[str], now: datetime, limit: Optional[int] = 2,
                requirements: Optional[Iterable[str]] = None, point=None) -> List[Dict]:
        """Proximity: same town first, then by straight-line distance from the patient's point (version 4) or the
        town centroid when known, then listed order.  Only currently valid sites able to perform the required
        services.  A `point` (lat, lon) from a stored patient location beats the town centroid."""
        valid = [s for s in self.sites(now) if self.capable(s, requirements)]
        origin = point or self.town_coords(town)
        if point:
            ordered = sorted(valid, key=lambda s: self.distance_km(origin, (s.get("lat"), s.get("lon"))) if s.get("lat") is not None else 1e9)
        else:
            same = [s for s in valid if town and s.get("town", "").lower() == town.lower()]
            rest = [s for s in valid if s not in same]
            if origin:
                rest.sort(key=lambda s: self.distance_km(origin, (s.get("lat"), s.get("lon"))) if s.get("lat") is not None else 1e9)
            ordered = same + rest
        return ordered if limit is None else ordered[:limit]

    # ---- version 4: distance, urgent care, mobile stops, portal ----------------------------------------
    def miles_from(self, origin, s: Dict) -> Optional[float]:
        """Straight-line miles from a point to a site (None when either side has no coordinates)."""
        if not origin or s.get("lat") is None:
            return None
        km = self.distance_km(origin, (s.get("lat"), s.get("lon")))
        return None if km >= 1e8 else round(km / 1.609344, 1)

    def nearest_urgent_care(self, origin, now: datetime) -> Optional[Dict]:
        """Closest partner-verified urgent care to the patient's point (or the first verified one without a point)."""
        valid = [u for u in self.urgent_care if u.get("verified_at") and u.get("verified_by")
                 and (now - datetime.fromisoformat(u["verified_at"])).days <= self.max_age_days]
        if not valid:
            return None
        if origin:
            valid.sort(key=lambda u: self.distance_km(origin, (u.get("lat"), u.get("lon"))))
        return valid[0]

    def mobile_stops(self, now: datetime, requirements: Optional[Iterable[str]] = None, town: Optional[str] = None,
                     origin=None, within_miles: float = 8.0) -> List[Dict]:
        """Verified mobile-route stops that offer the required services and are in the patient's town or within
        `within_miles` of the patient's point.  Each result carries the route name and its distance."""
        out = []
        req = set(requirements or [])
        for r in self.mobile_routes:
            v = r.get("verified_at")
            if not v or not r.get("verified_by") or (now - datetime.fromisoformat(v)).days > self.max_age_days:
                continue
            if req and not req <= set(r.get("services") or []):
                continue
            for st in r.get("stops", []):
                same_town = bool(town) and (st.get("town") or "").lower() == town.lower()
                pt = self.town_coords(st.get("town"))
                d = self.miles_from(origin or self.town_coords(town), {"lat": pt[0], "lon": pt[1]}) if pt else None
                if same_town or (d is not None and d <= within_miles):
                    out.append(dict(st, route_id=r["id"], route_name=r["name"], miles=d, services=r.get("services") or []))
        out.sort(key=lambda x: (x["miles"] if x["miles"] is not None else 1e9))
        return out

    def portal(self, now: datetime) -> Optional[Dict]:
        """The partner's approved patient-portal link (name + link), or None when missing/unapproved/expired."""
        pp = self.patient_portal
        if not pp.get("link") or not pp.get("approved_at") or not pp.get("approved_by"):
            return None
        if (now - datetime.fromisoformat(pp["approved_at"])).days > self.max_age_days:
            return None
        return {"name": pp.get("name") or "the patient portal", "link": pp["link"]}

    def town_coords(self, town: Optional[str]):
        if not town:
            return None
        for name, c in self.towns.items():
            if name.lower() == town.lower():
                return (c["lat"], c["lon"])
        return None

    @staticmethod
    def distance_km(a, b) -> float:
        import math
        if a is None or b is None or a[0] is None or b[0] is None:
            return 1e9
        lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
        h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
        return 2 * 6371.0 * math.asin(math.sqrt(h))

    def hours_for(self, s: Dict, weekday: Optional[str] = None, weekend: bool = False) -> str:
        """Hours trimmed to what the patient asked about: one weekday, the weekend, or everything (grouped)."""
        if weekday and s["hours"].get(weekday):
            span = s["hours"][weekday]
            return "%s %s-%s" % (weekday.title(), _short(span[0]), _short(span[1]))
        if weekend:
            parts = ["%s %s-%s" % (d.title(), _short(s["hours"][d][0]), _short(s["hours"][d][1])) for d in ("sat", "sun") if s["hours"].get(d)]
            if parts:
                return ", ".join(parts)
        return self.describe_hours(s)

    def describe_hours(self, s: Dict) -> str:
        groups = []
        for d in WEEKDAYS:
            span = s["hours"].get(d)
            if not span:
                continue
            key = "%s-%s" % (_short(span[0]), _short(span[1]))
            if groups and groups[-1][2] == key and WEEKDAYS.index(groups[-1][1]) == WEEKDAYS.index(d) - 1:
                groups[-1][1] = d
            else:
                groups.append([d, d, key])
        return ", ".join("%s %s" % (a.title() if a == b else "%s-%s" % (a.title(), b.title()), k) for a, b, k in groups)

    def open_on(self, s: Dict, weekday: str) -> bool:
        return bool(s["hours"].get(weekday))

    def next_date_for(self, weekday: str, now: datetime, s: Dict, horizon_days: int = 8) -> Optional[datetime]:
        """Next calendar date strictly after today on which `weekday` falls and the site is open."""
        if not self.open_on(s, weekday):
            return None
        target = WEEKDAYS.index(weekday)
        for delta in range(1, horizon_days):
            d = (now + timedelta(days=delta)).replace(hour=0, minute=0, second=0, microsecond=0)
            if d.weekday() == target:
                return d
        return None

    def describe(self, s: Dict, requirements: Optional[Iterable[str]] = None, latest_start: Optional[str] = None) -> str:
        """Compact hours: consecutive days with identical hours are grouped (Mon-Fri 7am-4pm).  A timed service's
        start limit is part of the description so no approved text can omit it."""
        groups = []
        for d in WEEKDAYS:
            span = s["hours"].get(d)
            if not span:
                continue
            key = "%s-%s" % (_short(span[0]), _short(span[1]))
            if groups and groups[-1][2] == key and WEEKDAYS.index(groups[-1][1]) == WEEKDAYS.index(d) - 1:
                groups[-1][1] = d
            else:
                groups.append([d, d, key])
        parts = ["%s %s" % (a.title() if a == b else "%s-%s" % (a.title(), b.title()), k) for a, b, k in groups]
        limit = self.latest_start(s, requirements, default=latest_start)
        hours = ", ".join(parts) + (("; must start by %s" % _short(limit)) if limit else "")
        req = (s.get("visit_requirements") or "").rstrip(". ")
        return "%s, %s (%s)%s" % (s["name"], s["address"], hours, (". " + req) if req else "")


def _parse(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def _short(hhmm: str) -> str:
    t = _parse(hhmm)
    suffix = "am" if t.hour < 12 else "pm"
    h = t.hour % 12 or 12
    return "%d%s%s" % (h, (":%02d" % t.minute) if t.minute else "", suffix)
