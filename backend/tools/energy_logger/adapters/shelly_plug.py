"""
Shelly adapter — built now, dormant until SHELLY_HOST is set.

Shelly plugs serve a local HTTP API with no auth on the LAN by default, and
there are two generations with different endpoints:

  Gen2/Plus/Pro  GET /rpc/Switch.GetStatus?id=0   -> {"apower": W, "aenergy": {"total": Wh}, "output": bool}
  Gen1           GET /status                      -> {"meters":[{"power": W,"total": Wmin}], "relays":[{"ison": bool}]}

Generation is detected once via GET /shelly and then cached, so no config is
needed beyond the IP address.

Note on units: Gen2 reports cumulative energy in watt-hours; Gen1's `total` is
in watt-minutes. Both are converted to kWh here so the sink never has to know
which generation produced a reading.
"""

from __future__ import annotations

import httpx

from ..base import AdapterError, DeviceAdapter, Reading

TIMEOUT = 8.0


class ShellyPlugAdapter(DeviceAdapter):
    kind = "shelly"

    def __init__(self, device_name: str, host: str, channel: int = 0):
        super().__init__(device_name)
        self.base = host.rstrip("/")
        if not self.base.startswith(("http://", "https://")):
            self.base = f"http://{self.base}"
        self.channel = channel
        self._gen: int | None = None

    async def _detect_gen(self, client: httpx.AsyncClient) -> int:
        if self._gen is not None:
            return self._gen
        try:
            r = await client.get(f"{self.base}/shelly")
            self._gen = int(r.json().get("gen", 1)) if r.status_code == 200 else 1
        except Exception:
            self._gen = 1
        return self._gen

    async def read(self) -> Reading:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                gen = await self._detect_gen(client)
                if gen >= 2:
                    r = await client.get(
                        f"{self.base}/rpc/Switch.GetStatus", params={"id": self.channel}
                    )
                    r.raise_for_status()
                    d = r.json()
                    watts = _as_float(d.get("apower"))
                    total_wh = _as_float((d.get("aenergy") or {}).get("total"))
                    kwh = total_wh / 1000 if total_wh is not None else None
                    is_on = d.get("output")
                    # the plug also reports these every poll; dropping them
                    # threw away most of what it measures
                    detail = {
                        "voltage": _as_float(d.get("voltage")),
                        "current": _as_float(d.get("current")),
                        "freq": _as_float(d.get("freq")),
                        "temp_c": _as_float((d.get("temperature") or {}).get("tC")),
                    }
                else:
                    r = await client.get(f"{self.base}/status")
                    r.raise_for_status()
                    d = r.json()
                    meters = d.get("meters") or []
                    relays = d.get("relays") or []
                    meter = meters[self.channel] if self.channel < len(meters) else {}
                    watts = _as_float(meter.get("power"))
                    # Gen1 `total` is watt-minutes.
                    total_wm = _as_float(meter.get("total"))
                    kwh = total_wm / 60000 if total_wm is not None else None
                    is_on = relays[self.channel].get("ison") if self.channel < len(relays) else None
                    detail = {"temp_c": _as_float(d.get("temperature"))}
        except httpx.HTTPError as e:
            raise AdapterError(f"{self.base} unreachable: {type(e).__name__}: {e}") from e
        except Exception as e:
            raise AdapterError(f"{self.base} bad response: {type(e).__name__}: {e}") from e

        if watts is None and kwh is None:
            raise AdapterError(f"{self.base} returned no energy fields")

        extra = {"host": self.base, "gen": gen}
        if is_on is not None:
            extra["is_on"] = bool(is_on)
        for k, v in (detail or {}).items():
            if v is not None:
                extra[k] = v

        return Reading(
            device_name=self.device_name,
            source=self.source,
            taken_at=Reading.now(),
            watts=watts,
            # Shelly's counter is lifetime, not per-day. The sink works from
            # deltas, so this is still the right field for it to difference —
            # it simply never resets at midnight the way Kasa's does.
            kwh_today=kwh,
            extra=extra,
        )


def _as_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
