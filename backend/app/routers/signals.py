"""
GET /api/signals?lat=&lon=

Live location-specific signals for the prediction panel. Requires real
coordinates — there is no default location, because a signal for somewhere the
user isn't would be worse than no signal at all.
"""

from fastapi import APIRouter, HTTPException

from app.regional import from_browser
from app.signals import location_signals

router = APIRouter()


@router.get("")
async def get_signals(lat: float | None = None, lon: float | None = None, region: str | None = None):
    if lat is None or lon is None:
        raise HTTPException(
            status_code=400,
            detail="Coordinates required. Kroven does not substitute a default location.",
        )
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="Coordinates out of range.")

    payload = await location_signals(lat, lon)

    # Rate signals only mean something inside the covered service area.
    if region is not None:
        loc = from_browser(region)
        payload["service_area"] = loc.status
        if loc.status != "bay_area":
            payload["signals"] = [s for s in payload["signals"] if s["key"] != "rate_window"]
            payload["note"] = (
                "Pricing signals are hidden: Kroven's verified rate data covers the Bay Area "
                "beta only. Weather and grid readings below are still live for your location."
            )
    return payload
