"""
Service-area scoping. The Kroven beta covers the nine-county Bay Area only.

Why it's this narrow: routers/rates.py is a real PG&E time-of-use schedule.
Quoting it to someone in San Diego or Texas would produce confident, wrong
dollar figures, so anyone outside the service area gets told plainly instead.

Location arrives one of two ways:
  1. Browser geolocation, reverse-geocoded client-side into
     "city|county|state|countryCode" (or a "__sentinel__" when it failed).
  2. The person just typing where they are, when the browser blocks location.
     scan_message() reads a city name or ZIP straight out of what they wrote.

There is deliberately NO area-average fallback. If we do not know a household's
usage we ask them for it; we never estimate their consumption from a regional
mean and present it as insight.
"""

import os
import re
from typing import Literal, NamedTuple

LocationStatus = Literal[
    "bay_area",       # in the service area
    "outside",        # located, but not the Bay Area
    "denied",         # browser is blocking location
    "unavailable",    # geolocation failed or isn't supported
    "unknown",        # never attempted / nothing to go on
]

BAY_AREA_COUNTIES = {
    "alameda", "contra costa", "marin", "napa", "san francisco",
    "san mateo", "santa clara", "solano", "sonoma",
}

# Used when a county isn't available, and to read a typed message.
BAY_AREA_CITIES = {
    "san francisco", "sf", "oakland", "berkeley", "san jose", "palo alto",
    "mountain view", "sunnyvale", "santa clara", "cupertino", "fremont",
    "hayward", "san mateo", "redwood city", "daly city", "richmond",
    "walnut creek", "concord", "alameda", "emeryville", "albany", "el cerrito",
    "san leandro", "union city", "newark", "milpitas", "campbell", "saratoga",
    "los gatos", "los altos", "menlo park", "atherton", "burlingame",
    "san carlos", "belmont", "foster city", "millbrae", "south san francisco",
    "san bruno", "pacifica", "half moon bay", "san rafael", "novato",
    "mill valley", "sausalito", "larkspur", "corte madera", "tiburon",
    "petaluma", "santa rosa", "rohnert park", "sonoma", "napa", "vallejo",
    "fairfield", "vacaville", "benicia", "martinez", "pleasant hill",
    "danville", "san ramon", "dublin", "pleasanton", "livermore", "antioch",
    "pittsburg", "brentwood", "orinda", "lafayette", "moraga", "hercules",
    "pinole", "san pablo", "castro valley", "gilroy", "morgan hill",
    "east palo alto", "stanford", "brisbane", "colma", "hillsborough",
    "woodside", "portola valley", "sebastopol", "healdsburg", "windsor",
    "american canyon", "suisun city", "dixon", "rio vista", "clayton",
}

# Approximate — county or city is preferred whenever available. Deliberately
# excludes 942 (Sacramento), 952/953 (Sacramento, Central Valley).
BAY_AREA_ZIP_PREFIXES = {
    "940", "941", "943", "944", "945", "946", "947", "948", "949", "950",
    "951", "954",
}

_SENTINELS = {
    "__denied__": "denied",
    "__unavailable__": "unavailable",
    "__unsupported__": "unavailable",
}

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


class Location(NamedTuple):
    status: LocationStatus
    label: str = ""      # e.g. "Oakland, Alameda County" — for talking to the user
    source: str = ""     # "browser" | "typed" | ""


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _strip_county(name: str) -> str:
    return re.sub(r"\s+county$", "", _norm(name))


def from_browser(region: str | None) -> Location:
    """Interpret the client's 'city|county|state|country' string."""
    if not region or not region.strip():
        return Location("unknown")

    key = region.strip()
    if key in _SENTINELS:
        return Location(_SENTINELS[key])

    parts = [p.strip() for p in key.split("|")]
    city = parts[0] if len(parts) > 0 else ""
    county = parts[1] if len(parts) > 1 else ""
    state = parts[2] if len(parts) > 2 else ""
    country = parts[3] if len(parts) > 3 else ""

    if country and country.upper() not in ("US", "USA"):
        return Location("outside", city or state or country, "browser")

    label = ", ".join([p for p in (city, county) if p]) or state
    if county and _strip_county(county) in BAY_AREA_COUNTIES:
        return Location("bay_area", label, "browser")
    if city and _norm(city) in BAY_AREA_CITIES:
        return Location("bay_area", label, "browser")
    if city or county or state:
        return Location("outside", label or state, "browser")
    return Location("unknown")


def scan_message(text: str | None) -> Location:
    """Pull a city or ZIP out of something the person typed."""
    if not text:
        return Location("unknown")

    lowered = _norm(text)

    for zip_code in _ZIP_RE.findall(text):
        if zip_code[:3] in BAY_AREA_ZIP_PREFIXES:
            return Location("bay_area", zip_code, "typed")
        return Location("outside", zip_code, "typed")

    # Longest names first so "south san francisco" wins over "san francisco".
    for city in sorted(BAY_AREA_CITIES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(city)}\b", lowered):
            return Location("bay_area", city.title(), "typed")

    return Location("unknown")


def resolve(region: str | None, message: str | None) -> Location:
    """Browser location wins; fall back to whatever they typed."""
    browser = from_browser(region)
    if browser.status in ("bay_area", "outside"):
        return browser

    typed = scan_message(message)
    if typed.status in ("bay_area", "outside"):
        return typed

    return browser  # preserves denied/unavailable/unknown
