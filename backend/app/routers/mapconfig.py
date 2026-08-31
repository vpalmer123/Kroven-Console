"""
GET /api/mapconfig

Hands the browser the public Mapbox token, if one is configured.

The token lives in the Railway environment, not in the repo and not in the
frontend source. Vic sets it himself; nobody has to paste a credential into a
chat or a commit. A Mapbox *public* token (pk.*) is designed to be visible in
client-side code, but it should still be URL-restricted in the Mapbox dashboard
so it only works from the Kroven domain.

With no token set this returns enabled=false and the console keeps using the
built-in canvas renderer. The map degrades, nothing breaks.
"""

import os

from fastapi import APIRouter

router = APIRouter()

BAY_AREA_BOUNDS = [[-123.20, 36.85], [-121.20, 38.60]]   # SW, NE — the beta box


@router.get("")
def map_config():
    token = os.environ.get("MAPBOX_TOKEN", "").strip()
    if not token:
        return {
            "enabled": False,
            "reason": "No MAPBOX_TOKEN set. The console falls back to its built-in "
                      "renderer, which needs no key.",
        }
    if not token.startswith("pk."):
        # A secret token (sk.*) must never be handed to a browser.
        return {
            "enabled": False,
            "reason": "MAPBOX_TOKEN is not a public token. Only a pk.* token may be "
                      "sent to the browser; replace it with a public token.",
        }
    return {
        "enabled": True,
        "token": token,
        "style": os.environ.get("MAPBOX_STYLE", "mapbox://styles/mapbox/dark-v11"),
        "center": [-122.4194, 37.7749],
        "bounds": BAY_AREA_BOUNDS,
        "min_zoom": 7.5,
        "max_zoom": 18,
    }
