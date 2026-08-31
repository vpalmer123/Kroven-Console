"""
Kroven energy logger — long-lived poller feeding real device data into
Supabase `energy_readings` for LSTM training.

    python -m tools.energy_logger.main --check     # one poll, print, write nothing
    python -m tools.energy_logger.main             # run forever

Every device is polled independently: one plug going offline never stops the
others, and never stops the process. Failures back off, then recover on their
own.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a plain script as well as a module.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.energy_logger.adapters import build_adapters          # noqa: E402
from tools.energy_logger.base import AdapterError, DeviceAdapter  # noqa: E402
from tools.energy_logger.sink import SupabaseSink                 # noqa: E402

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
MAX_BACKOFF = 15 * 60
_stop = asyncio.Event()


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts}Z {msg}", flush=True)


class DeviceState:
    """Per-device health, so one flaky plug backs off on its own."""

    def __init__(self, adapter: DeviceAdapter):
        self.adapter = adapter
        self.failures = 0
        self.skip_until = 0.0
        self.ok_count = 0

    def note_ok(self):
        if self.failures:
            log(f"[{self.adapter.device_name}] recovered after {self.failures} failure(s)")
        self.failures = 0
        self.skip_until = 0.0
        self.ok_count += 1

    def note_fail(self, loop_time: float, err: str):
        self.failures += 1
        backoff = min(POLL_SECONDS * (2 ** min(self.failures, 6)), MAX_BACKOFF)
        self.skip_until = loop_time + backoff
        log(f"[{self.adapter.device_name}] poll failed ({self.failures}): {err}")
        log(f"[{self.adapter.device_name}] backing off {int(backoff)}s")


async def poll_once(states: list[DeviceState], sink: SupabaseSink, dry_run: bool) -> int:
    loop_time = asyncio.get_running_loop().time()
    rows = []

    for st in states:
        if st.skip_until and loop_time < st.skip_until:
            continue
        try:
            reading = await st.adapter.read()
        except AdapterError as e:
            st.note_fail(loop_time, str(e))
            continue
        except Exception as e:  # never let an adapter bug kill the loop
            st.note_fail(loop_time, f"unexpected {type(e).__name__}: {e}")
            continue

        st.note_ok()
        if not dry_run:
            sink.write_observations(reading)   # every signal, not just kWh
        row, method = sink.to_row(reading)
        w = f"{reading.watts:.1f} W" if reading.watts is not None else "— W"
        k = f"{reading.kwh_today:.3f} kWh today" if reading.kwh_today is not None else "— kWh"
        if row is None:
            log(f"[{reading.device_name}] {w}, {k} ({method}, nothing to write yet)")
        else:
            log(f"[{reading.device_name}] {w}, {k} -> {row['kwh_consumed']:.6f} kWh ({method})")
            rows.append(row)

    if dry_run:
        return len(rows)
    if rows:
        sink.write(rows)
    return len(rows)


async def run(dry_run: bool) -> int:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    household = os.environ.get("KROVEN_HOUSEHOLD_ID", "").strip()

    if not dry_run and not (url and key):
        log("SUPABASE_URL and SUPABASE_SERVICE_KEY are required to write. Use --check to test devices only.")
        return 2
    if not dry_run and not household:
        log("KROVEN_HOUSEHOLD_ID is required so readings attach to your household.")
        log("Get it from the browser console on the app: localStorage.getItem('kroven_household_id')")
        return 2

    adapters, notes = build_adapters()
    for n in notes:
        log(f"[config] {n}")
    if not adapters:
        log("No devices configured. Set KASA_HOST (and later SHELLY_HOST).")
        return 2

    sink = SupabaseSink(url or "http://unused", key or "unused", household or "dry-run", log=log)
    if not dry_run:
        cols = sink.detect_columns()
        log(f"[sink] energy_readings columns: {sorted(cols)}")
        for optional in ("device_name", "watts", "kwh_today"):
            if optional not in cols:
                log(f"[sink] '{optional}' column absent — run migration 003 to store it "
                    f"(device stays identifiable via 'source' meanwhile)")
        existing = sink.count_rows()
        if existing is not None:
            log(f"[sink] household '{household}' currently has {existing} readings")

    states = [DeviceState(a) for a in adapters]
    for st in states:
        log(f"[config] {st.adapter.describe()}")

    if dry_run:
        log("--check: polling once, writing nothing")
        await poll_once(states, sink, dry_run=True)
        for st in states:
            await st.adapter.close()
        sink.close()
        return 0

    log(f"Polling every {POLL_SECONDS}s. Ctrl-C to stop.")
    written = 0
    try:
        while not _stop.is_set():
            sink.flush_buffer()
            written += await poll_once(states, sink, dry_run=False)
            try:
                await asyncio.wait_for(_stop.wait(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        log(f"Stopping. {written} readings written this run.")
        for st in states:
            await st.adapter.close()
        sink.close()
    return 0


def _install_signals() -> None:
    def handler(*_):
        _stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, AttributeError, OSError):
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Kroven multi-device energy logger")
    ap.add_argument("--check", action="store_true",
                    help="poll every configured device once, print results, write nothing")
    args = ap.parse_args()

    for candidate in (Path(__file__).resolve().parents[2] / ".env", Path(".env")):
        if candidate.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(candidate)
                log(f"[config] loaded {candidate}")
                break
            except ImportError:
                pass

    _install_signals()
    try:
        return asyncio.run(run(dry_run=args.check))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
