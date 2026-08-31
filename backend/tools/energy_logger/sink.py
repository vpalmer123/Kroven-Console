"""
Writes readings into Supabase `energy_readings` — the table the forecaster
actually reads (see backend/app/routers/forecast.py, which pulls
energy_readings and feeds them to predict_next).

Deliberately NOT household_profiles: its primary key is household_id, one row
per household, so a per-minute logger would collide or overwrite on every
write. That table holds profile facts, not a time series.

Two things this handles that a naive insert would not:

  * Interval energy. Devices report a cumulative counter; the model wants the
    energy used during each interval. We difference successive counter values
    and cope with the counter resetting (Kasa's resets at midnight).
  * Schema drift. device_name/watts columns only exist after migration 003.
    The columns present are detected at startup and anything unsupported is
    folded into `source`, so the logger runs correctly either way.

Failed writes are buffered to disk and retried, so a Supabase blip does not
cost hours of collection.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .base import Reading

BUFFER_PATH = Path(os.environ.get("LOGGER_BUFFER", "energy_logger_buffer.jsonl"))
TABLE = "energy_readings"


class SupabaseSink:
    def __init__(self, url: str, key: str, household_id: str, log=print):
        self.url = url.rstrip("/")
        self.key = key
        self.household_id = household_id
        self.log = log
        self.columns: set[str] = set()
        self._last: dict[str, tuple[datetime, float]] = {}   # source -> (when, counter)
        self._client = httpx.Client(
            timeout=20,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
        )

    # ---------- schema ----------

    def detect_columns(self) -> set[str]:
        try:
            r = self._client.get(f"{self.url}/rest/v1/")
            spec = r.json()
            defs = spec.get("definitions") or spec.get("components", {}).get("schemas", {})
            props = (defs.get(TABLE) or {}).get("properties") or {}
            self.columns = set(props)
        except Exception as e:
            self.log(f"[sink] could not read schema ({type(e).__name__}); assuming base columns")
            self.columns = {
                "household_id", "recorded_at", "kwh_consumed", "source",
            }
        return self.columns

    # ---------- conversion ----------

    def _interval_kwh(self, r: Reading) -> tuple[float | None, str]:
        """Energy used since the previous sample, and how it was derived."""
        prev = self._last.get(r.source)

        if r.kwh_today is not None:
            self._last[r.source] = (r.taken_at, r.kwh_today)
            if prev is None:
                return None, "priming"          # first sample: no interval yet
            _, prev_kwh = prev
            delta = r.kwh_today - prev_kwh
            if delta >= 0:
                return delta, "counter-delta"
            # Counter went backwards: midnight reset (Kasa) or device reboot.
            # The new value is energy accumulated since that reset.
            return max(r.kwh_today, 0.0), "counter-reset"

        if r.watts is not None:
            if prev is None:
                self._last[r.source] = (r.taken_at, 0.0)
                return None, "priming"
            prev_when, _ = prev
            hours = (r.taken_at - prev_when).total_seconds() / 3600
            self._last[r.source] = (r.taken_at, 0.0)
            if hours <= 0:
                return None, "bad-interval"
            return (r.watts / 1000) * hours, "power-integrated"

        return None, "no-energy-data"

    def to_row(self, r: Reading) -> tuple[dict | None, str]:
        kwh, method = self._interval_kwh(r)
        if kwh is None:
            return None, method

        row: dict = {
            "household_id": self.household_id,
            "recorded_at": r.taken_at.isoformat(),
            "kwh_consumed": round(kwh, 6),
        }

        # Only send columns that exist; otherwise keep the device identifiable
        # through `source`, which the base schema always has.
        if "device_name" in self.columns:
            row["device_name"] = r.device_name
        if "watts" in self.columns and r.watts is not None:
            row["watts"] = round(r.watts, 3)
        if "kwh_today" in self.columns and r.kwh_today is not None:
            row["kwh_today"] = round(r.kwh_today, 6)
        if "source" in self.columns:
            row["source"] = r.source

        return row, method

    # ---------- writing ----------

    def write_observations(self, reading) -> int:
        """Store every signal a device reported, not just the energy delta.

        energy_readings only has room for kWh, so voltage, current, frequency,
        temperature and switch state were being read from the Shelly and then
        dropped. They go to `observations`, which is shaped for exactly this.
        Missing table is not an error - the logger still works without it.
        """
        extra = reading.extra or {}
        signals = []
        if reading.watts is not None:
            signals.append(("power_w", float(reading.watts), "W"))
        if reading.kwh_today is not None:
            signals.append(("energy_kwh_total", float(reading.kwh_today), "kWh"))
        for key, sig, unit in (
            ("voltage", "voltage_v", "V"),
            ("current", "current_a", "A"),
            ("freq", "frequency_hz", "Hz"),
            ("temp_c", "temperature_c", "C"),
            ("amplitude_variance", "csi_variance", "var"),
            ("rssi_mean", "rssi_dbm", "dBm"),
        ):
            if extra.get(key) is not None:
                try:
                    signals.append((sig, float(extra[key]), unit))
                except (TypeError, ValueError):
                    pass
        if extra.get("is_on") is not None:
            signals.append(("switch_state", 1.0 if extra["is_on"] else 0.0, "bool"))

        if not signals:
            return 0

        stamp = reading.taken_at.isoformat()
        rows = [{
            "household_id": self.household_id,
            "observed_at": stamp,
            "source": reading.source,
            "signal_type": sig,
            "value": val,
            "meta": {"unit": unit, "device": reading.device_name},
        } for sig, val, unit in signals]

        try:
            resp = self._client.post(
                f"{self.url}/rest/v1/observations"
                f"?on_conflict=household_id,source,signal_type,observed_at",
                content=json.dumps(rows),
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            if resp.status_code >= 300:
                if not getattr(self, "_obs_warned", False):
                    self.log(f"[sink] observations unavailable ({resp.status_code}); "
                             f"run migration 004 to keep the richer signals")
                    self._obs_warned = True
                return 0
        except Exception:
            return 0
        return len(rows)

    def write(self, rows: list[dict]) -> bool:
        if not rows:
            return True
        try:
            resp = self._client.post(f"{self.url}/rest/v1/{TABLE}", content=json.dumps(rows))
            if resp.status_code >= 300:
                self.log(f"[sink] insert rejected {resp.status_code}: {resp.text[:200]}")
                self._buffer(rows)
                return False
            return True
        except Exception as e:
            self.log(f"[sink] insert failed ({type(e).__name__}); buffering")
            self._buffer(rows)
            return False

    def _buffer(self, rows: list[dict]) -> None:
        try:
            with BUFFER_PATH.open("a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
        except Exception as e:
            self.log(f"[sink] could not buffer to disk: {type(e).__name__}")

    def flush_buffer(self) -> int:
        """Retry anything written to disk while the database was unreachable."""
        if not BUFFER_PATH.exists():
            return 0
        try:
            lines = [l for l in BUFFER_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
        except Exception:
            return 0
        if not lines:
            return 0

        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        try:
            resp = self._client.post(f"{self.url}/rest/v1/{TABLE}", content=json.dumps(rows))
            if resp.status_code < 300:
                BUFFER_PATH.unlink(missing_ok=True)
                self.log(f"[sink] flushed {len(rows)} buffered readings")
                return len(rows)
            self.log(f"[sink] buffer flush rejected {resp.status_code}; keeping file")
        except Exception as e:
            self.log(f"[sink] buffer flush failed ({type(e).__name__}); keeping file")
        return 0

    def count_rows(self) -> int | None:
        try:
            r = self._client.get(
                f"{self.url}/rest/v1/{TABLE}",
                params={"select": "id", "household_id": f"eq.{self.household_id}"},
                headers={"Prefer": "count=exact", "Range": "0-0"},
            )
            rng = r.headers.get("content-range", "")
            if "/" in rng:
                return int(rng.split("/")[-1])
        except Exception:
            pass
        return None

    def close(self):
        self._client.close()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
