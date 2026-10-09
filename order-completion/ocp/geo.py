"""Version 4: where is the patient?  (See the v4 brief §3.)

Sources, most to least reliable, all stored with their source and an expiry:
  feed_address   the partner feed's home town (or address/zip when it carries one), geocoded once
  patient_town   a town the patient named in a text
  patient_zip    a zip the patient named in a text
  patient_shared browser geolocation from the consent page (/where/<token>), single use
No carrier location exists for A2P SMS senders; IP geolocation from a link click is not used.

The Geocoder here is a local table of synthetic centroids.  Production swaps in the Census Bureau geocoder
(free, keyless) or a paid service behind the same interface.  Nothing here calls the network.
"""
from __future__ import annotations

import json
import math
import os
from typing import Dict, Optional, Tuple

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "zip_centroids.json")
KM_PER_MILE = 1.609344


def distance_km(a, b) -> float:
    if a is None or b is None or a[0] is None or b[0] is None:
        return 1e9
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def miles(km: float) -> float:
    return km / KM_PER_MILE


class Geocoder:
    """Interface: text (zip or town) -> (lat, lon, label) or None."""
    name = "base"
    simulated = True

    def geocode(self, text: str) -> Optional[Tuple[float, float, str]]:  # pragma: no cover - interface
        raise NotImplementedError


class LocalGeocoder(Geocoder):
    """Synthetic centroid table plus the directory's towns.  Case-insensitive town match; 5-digit zip match."""
    name = "local-centroids"

    def __init__(self, towns: Optional[Dict] = None, path: str = DEFAULT_PATH):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {"zips": {}, "towns_extra": {}}
        self.zips: Dict[str, Dict] = data.get("zips", {})
        self.towns: Dict[str, Dict] = dict(data.get("towns_extra", {}))
        for name, c in (towns or {}).items():
            self.towns[name] = c

    def geocode(self, text: str) -> Optional[Tuple[float, float, str]]:
        t = (text or "").strip()
        if not t:
            return None
        if t.isdigit() and len(t) == 5:
            z = self.zips.get(t)
            return (z["lat"], z["lon"], "%s (%s)" % (t, z["town"])) if z else None
        for name, c in self.towns.items():
            if name.lower() == t.lower():
                return (c["lat"], c["lon"], name)
        return None


class CensusGeocoder(Geocoder):
    """Placeholder for production: https://geocoding.geo.census.gov/geocoder/ (free, no key).  NOT exercised here;
    the prototype never makes network calls.  Kept so the interface and the swap point are explicit."""
    name = "census"
    simulated = False

    def geocode(self, text: str):  # pragma: no cover
        raise NotImplementedError("network geocoding is not enabled in the prototype")
