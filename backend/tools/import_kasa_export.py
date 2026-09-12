"""Import a Kasa energy-monitoring export (.zip of .xls) into energy_readings.

The vendor app exports what the plug measured; the logger records what Kroven
observed. When the logger is down, the export is the only record of that
period, and this is how it gets back in.

    python -m tools.import_kasa_export <export.zip> --device <device_id>
    ... --commit            actually write (default is a dry run)

WHY EVERY ROW IS TAGGED BY SOURCE
Each row records where it came from: the device, that it was a manual export
rather than a live observation, and which resolution. That matters because
these are not equivalent to logger rows — they are the vendor's own
aggregation, and hourly kWh is not a 5-minute sample. Tagging keeps the
distinction visible downstream instead of blending two kinds of evidence into
one indistinguishable series, and makes an import reversible by deleting one
source.

WHY THE DEVICE IS AN ARGUMENT AND NOT A GUESS
A plug's role changes when it is physically moved, and whether its trace is one
appliance or a whole extension cord decides what may legitimately be modelled
from it. The registry knows the current role; a filename does not. So the
device is named explicitly and its signal_type is read from the registry rather
than inferred here.

Existing rows are never duplicated: import is keyed on (source, recorded_at),
and anything already present is skipped and reported.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import xlrd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Which sheet in which workbook means what. Anything not listed is ignored
# rather than guessed at: the Month and Year sheets are roll-ups of the same
# measurements, and importing them alongside the hourly series would double
# count the same energy.
SHEETS = {
    ("Energy Usage.xls", "Day"):  ("energy_hourly", "kwh", 3600),
    ("Power.xls", "Day"):         ("power_5min", "watts", 300),
    # Power/Week is deliberately excluded. It is one instantaneous sample per
    # hour over the same window Energy Usage/Day already covers as measured
    # kWh, so importing it both double counts the period and does so with the
    # weaker of the two series: 14.077 kWh against 18.142 kWh for identical
    # hours, because a single sample cannot see what happened between samples.
}


def headers() -> dict:
    return {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
            "Content-Type": "application/json"}


def parse_cell_time(raw, datemode) -> datetime | None:
    """A timestamp as the export writes it — text or an Excel serial."""
    if isinstance(raw, float):
        try:
            t = xlrd.xldate_as_tuple(raw, datemode)
            return datetime(*t)
        except Exception:
            return None
    s = str(raw).strip()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y/%m"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def read_export(zip_path: Path) -> dict[str, list[tuple[datetime, float]]]:
    """Every series the export contains, keyed by the kind of measurement."""
    out: dict[str, list[tuple[datetime, float]]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        for (fname, sheet), (kind, _unit, _step) in SHEETS.items():
            path = Path(tmp) / fname
            if not path.exists():
                continue
            book = xlrd.open_workbook(path)
            if sheet not in book.sheet_names():
                continue
            sh = book.sheet_by_name(sheet)
            series = []
            # Row 0 is the range header and the column title, not a reading.
            for r in range(1, sh.nrows):
                ts = parse_cell_time(sh.cell_value(r, 0), book.datemode)
                val = sh.cell_value(r, 1)
                if ts is None or not isinstance(val, (int, float)):
                    continue
                series.append((ts, float(val)))
            if series:
                out[kind] = series
    return out


def to_rows(kind: str, series, household: str, source: str, tz_offset_hours: float):
    """Readings in the shape energy_readings stores.

    Power samples are converted to the energy used over their own interval,
    which is what the column means; a watt reading is not a kilowatt-hour and
    storing it as one would inflate every total downstream.
    """
    step = {"energy_hourly": 3600, "power_5min": 300}[kind]
    rows = []
    for ts, val in series:
        # The export writes local wall-clock time with no zone. Stamping it as
        # UTC would shift every reading by the offset and silently misalign it
        # against the logger's own rows.
        utc = ts.replace(tzinfo=timezone.utc) - _offset(tz_offset_hours)
        kwh = val if kind == "energy_hourly" else (val * step / 3600.0) / 1000.0
        rows.append({
            "household_id": household,
            "recorded_at": utc.isoformat(),
            "kwh_consumed": round(kwh, 6),
            "source": f"{source}:{kind}",
        })
    return rows


def _offset(hours: float):
    from datetime import timedelta
    return timedelta(hours=hours)


def existing_keys(household: str, sources: list[str]) -> set[tuple[str, str]]:
    """What is already stored for these sources, so nothing is written twice."""
    seen: set[tuple[str, str]] = set()
    for src in sources:
        off = 0
        while True:
            r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/energy_readings",
                headers={**headers(), "Range": f"{off}-{off + 999}"},
                params={"select": "recorded_at,source", "source": f"eq.{src}",
                        "household_id": f"eq.{household}"},
                timeout=40,
            )
            batch = r.json()
            for x in batch:
                seen.add((x["source"], x["recorded_at"][:19]))
            if len(batch) < 1000:
                break
            off += 1000
    return seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path")
    ap.add_argument("--device", required=True, help="device id from the registry")
    ap.add_argument("--household", default=os.environ.get("KROVEN_HOUSEHOLD_ID", ""))
    ap.add_argument("--tz-offset", type=float, default=-7.0,
                    help="hours the export's local time is from UTC (PDT = -7)")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    if not SUPABASE_URL or not SERVICE_KEY:
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY not set", file=sys.stderr)
        return 2

    dev = httpx.get(f"{SUPABASE_URL}/rest/v1/devices", headers=headers(),
                    params={"select": "id,name,kind,signal_type,household_id",
                            "id": f"eq.{args.device}"}, timeout=30).json()
    if not dev:
        print(f"no device {args.device} in the registry", file=sys.stderr)
        return 2
    dev = dev[0]
    household = args.household or dev["household_id"]
    source = f"{dev['kind']}:{dev['name']}:manual_export"

    print(f"device   : {dev['name']} ({dev['kind']}, {dev['signal_type']})")
    print(f"household: {household}")
    print(f"source   : {source}:<kind>")
    print(f"timezone : export local time treated as UTC{args.tz_offset:+g}\n")

    series = read_export(Path(args.zip_path))
    if not series:
        print("nothing readable in that export", file=sys.stderr)
        return 1

    all_rows = []
    for kind, points in series.items():
        all_rows += to_rows(kind, points, household, source, args.tz_offset)

    seen = existing_keys(household, sorted({r["source"] for r in all_rows}))
    fresh, dupes = [], 0
    for r in all_rows:
        key = (r["source"], r["recorded_at"][:19])
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        fresh.append(r)

    by_src: dict[str, list] = {}
    for r in fresh:
        by_src.setdefault(r["source"], []).append(r)
    for src, rs in sorted(by_src.items()):
        span = f"{min(r['recorded_at'] for r in rs)[:16]} -> {max(r['recorded_at'] for r in rs)[:16]}"
        total = sum(r["kwh_consumed"] for r in rs)
        print(f"  {len(rs):>5} new  {src}\n         {span}   {total:.3f} kWh")
    print(f"\n  {dupes} already present, skipped")

    if not args.commit:
        print("\nDRY RUN - nothing written. Re-run with --commit to insert.")
        return 0
    if not fresh:
        print("\nnothing new to write.")
        return 0

    written = 0
    for i in range(0, len(fresh), 500):
        chunk = fresh[i:i + 500]
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/energy_readings",
                       headers={**headers(), "Prefer": "return=minimal"},
                       json=chunk, timeout=60)
        if r.status_code >= 300:
            print(f"insert failed at row {i}: {r.status_code} {r.text[:200]}",
                  file=sys.stderr)
            return 1
        written += len(chunk)
        print(f"  wrote {written}/{len(fresh)}")
    print(f"\ndone: {written} readings added.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
