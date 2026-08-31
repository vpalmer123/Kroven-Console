"""
Shelly Plug local-API poller for Kroven.

Polls the Shelly's local RPC endpoint on an interval and hands each reading to
an insert function.

READ THIS BEFORE WIRING IT INTO THE RAILWAY APP
-----------------------------------------------
SHELLY_IP is 10.0.0.113 — an RFC1918 private address on the house network. The
FastAPI app runs on Railway, in a datacentre. It has no route to that address
and never will, so starting this loop there produces a failed request every
POLL_INTERVAL_SECONDS forever and never a single reading.

The loop therefore refuses to start unless SHELLY_POLL_ENABLED is set, and it
probes the device once before committing. On Railway the probe fails and it logs
why and exits, instead of filling the logs with warnings.

Where this actually runs: tools/energy_logger, on a machine on the same LAN.
That process already polls Kasa and Shelly through the shared adapter interface
and writes to the same pool. This module exists so the same behaviour is
available in-process if the backend is ever run locally or given a LAN route
(VPN, tunnel, or a small agent forwarding readings up).
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("kroven.shelly")

# --- Config ---------------------------------------------------------
SHELLY_IP = os.environ.get("SHELLY_HOST", "10.0.0.113").replace("http://", "").strip("/")
SHELLY_URL = f"http://{SHELLY_IP}/rpc/Switch.GetStatus?id=0"
POLL_INTERVAL_SECONDS = int(os.environ.get("SHELLY_POLL_SECONDS", "30"))
DEVICE_LABEL = os.environ.get("SHELLY_DEVICE_NAME", "shelly_kroven")
ENABLED = os.environ.get("SHELLY_POLL_ENABLED", "").strip().lower() in ("1", "true", "yes")


async def fetch_shelly_status(client: httpx.AsyncClient) -> dict | None:
    """Hit the Shelly local API once. Returns parsed JSON or None on failure."""
    try:
        resp = await client.get(SHELLY_URL, timeout=5.0)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"Shelly poll failed: {e}")
        return None


def to_row(data: dict) -> dict:
    """Shape a Shelly response into a row matching the device-log schema."""
    return {
        "device": DEVICE_LABEL,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "power_w": data.get("apower"),
        "voltage_v": data.get("voltage"),
        "current_a": data.get("current"),
        "frequency_hz": data.get("freq"),
        "energy_wh_total": (data.get("aenergy") or {}).get("total"),
        "output_on": data.get("output"),
        "temp_c": (data.get("temperature") or {}).get("tC"),
    }


async def _reachable() -> bool:
    """One probe, so an unreachable device fails loudly once instead of hourly."""
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(f"http://{SHELLY_IP}/shelly")
            r.raise_for_status()
            info = r.json()
        logger.info(f"Shelly reachable: {info.get('model')} gen{info.get('gen')} at {SHELLY_IP}")
        return True
    except Exception as e:
        logger.error(
            f"Shelly at {SHELLY_IP} is not reachable from this host ({type(e).__name__}). "
            f"That address is on a private network; a cloud-hosted backend has no route "
            f"to it. Run the poller on a machine on that LAN instead. Not starting."
        )
        return False


async def shelly_polling_loop(supabase_insert_fn):
    """Runs forever. supabase_insert_fn(row: dict) writes one row."""
    if not await _reachable():
        return
    async with httpx.AsyncClient() as client:
        while True:
            data = await fetch_shelly_status(client)
            if data is not None:
                row = to_row(data)
                try:
                    await supabase_insert_fn(row)
                    logger.info(f"Shelly logged: {row['power_w']}W @ {row['timestamp']}")
                except Exception as e:
                    logger.error(f"Insert failed for Shelly row: {e}")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


def start_shelly_polling(supabase_insert_fn):
    """Start the loop as a background task. No-op unless explicitly enabled."""
    if not ENABLED:
        logger.info(
            "Shelly polling not started: SHELLY_POLL_ENABLED is not set. The device "
            "lives on a private network, so this only makes sense on a host with a "
            "LAN route. tools/energy_logger does this today."
        )
        return None
    task = asyncio.create_task(shelly_polling_loop(supabase_insert_fn))
    logger.info(f"Shelly polling started: {SHELLY_URL} every {POLL_INTERVAL_SECONDS}s")
    return task
