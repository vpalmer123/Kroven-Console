"""
Import manually-exported Kasa .xls files into `energy_readings`.

    python -m tools.energy_logger.import_kasa_export <file-or-zip> [--dry-run]

Accepts the .zip the Kasa app produces, a folder, or individual .xls files.
Safe to re-run on every new export: rows already present for this household at
the same timestamp are skipped, so nothing duplicates.

WHY NOT EVERY SHEET
-------------------
The two workbooks describe the SAME energy at four resolutions:

    Power.xls        Day    5-minute watts        <- finest
    Energy Usage.xls Day    hourly kWh
    Power.xls        Week   hourly watts
    Energy Usage.xls Month  daily kWh             <- aggregate
    Energy Usage.xls Year   monthly kWh           <- aggregate

Importing more than one of those for the same period would count the same
kilowatt-hours two or more times and teach the model a household that uses
several times what it really does. So:

  * 5-minute power is preferred wherever it exists (best resolution for the model),
  * hourly kWh fills only the hours the 5-minute data does not cover,
  * Month and Year are never imported — they are roll-ups of data already taken,
  * hourly *watts* (Power/Week) is ignored because the hourly kWh series measures
    the same hours directly rather than inferring energy from a spot wattage.

OTHER DETAILS
-------------
  * "/" means the device reported nothing for that slot; those rows are skipped.
  * Timestamps in the export are device-local wall clock with no zone. They are
    localised with EXPORT_TIMEZONE (default America/Los_Angeles) before being
    stored, otherwise every reading would land up to 8 hours off.
  * Wattage is converted with the real gap to the next sample, not an assumed
    5 minutes, so a gap in the export cannot silently inflate energy.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from tools.energy_logger.sink import SupabaseSink  # noqa: E402

SOURCE = "kasa:PS5:manual_export"
NO_DATA = {"/", "", "-", "--", "N/A"}
MAX_SANE_GAP = timedelta(hours=2)   # beyond this, assume a reporting gap


def log(msg: str) -> None:
    print(msg, flush=True)


def _tz():
    name = os.environ.get("EXPORT_TIMEZONE", "America/Los_Angeles")
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        log(f"[warn] unknown EXPORT_TIMEZONE {name!r}; treating timestamps as UTC")
        return None


def parse_stamp(raw, tz) -> datetime | None:
    """Kasa writes '2026/08/29 18:10:00', or '2026/08/29' on daily sheets."""
    if isinstance(raw, float):
        return None
    text = str(raw).strip()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=tz) if tz else dt
        except ValueError:
            continue
    return None


def read_sheet(path: Path, sheet_name: str) -> list[tuple[datetime, float]]:
    """Return (timestamp, value) pairs, dropping '/' and unparseable rows."""
    import xlrd

    tz = _tz()
    wb = xlrd.open_workbook(str(path))
    if sheet_name not in wb.sheet_names():
        return []
    sh = wb.sheet_by_name(sheet_name)

    out: list[tuple[datetime, float]] = []
    skipped_nodata = 0
    for r in range(1, sh.nrows):          # row 0 is the range header
        stamp = parse_stamp(sh.cell_value(r, 0), tz)
        if stamp is None:
            continue
        raw = sh.cell_value(r, 1)
        if isinstance(raw, str):
            if raw.strip() in NO_DATA:
                skipped_nodata += 1
                continue
            try:
                raw = float(raw.strip())
            except ValueError:
                skipped_nodata += 1
                continue
        out.append((stamp, float(raw)))

    log(f"    {path.name} [{sheet_name}]: {len(out)} usable rows, {skipped_nodata} skipped (no data)")
    return out


def power_to_rows(samples: list[tuple[datetime, float]]) -> list[dict]:
    """5-minute wattage -> per-interval kWh, using the real gap between samples."""
    rows = []
    for i, (stamp, watts) in enumerate(samples):
        if i + 1 < len(samples):
            gap = samples[i + 1][0] - stamp
        else:
            gap = samples[i][0] - samples[i - 1][0] if i else timedelta(minutes=5)
        if gap <= timedelta(0) or gap > MAX_SANE_GAP:
            gap = timedelta(minutes=5)
        kwh = (watts / 1000.0) * (gap.total_seconds() / 3600.0)
        rows.append({"recorded_at": stamp, "kwh": kwh, "watts": watts,
                     "src": f"{SOURCE}:power_5min"})
    return rows


def energy_to_rows(samples: list[tuple[datetime, float]]) -> list[dict]:
    """Hourly kWh is already energy — take it as measured."""
    return [{"recorded_at": s, "kwh": v, "watts": None, "src": f"{SOURCE}:energy_hourly"}
            for s, v in samples]


def collect(paths: list[Path]) -> list[dict]:
    power_file = next((p for p in paths if p.name.lower().startswith("power")), None)
    energy_file = next((p for p in paths if "energy" in p.name.lower()), None)

    rows: list[dict] = []
    covered_hours: set[datetime] = set()

    if power_file:
        log("  reading 5-minute power (preferred resolution)")
        samples = read_sheet(power_file, "Day")
        rows.extend(power_to_rows(samples))
        for s, _ in samples:
            covered_hours.add(s.replace(minute=0, second=0, microsecond=0))

    if energy_file:
        log("  reading hourly energy (fills hours the 5-minute data misses)")
        hourly = read_sheet(energy_file, "Day")
        kept, dropped = [], 0
        for stamp, val in hourly:
            if stamp.replace(minute=0, second=0, microsecond=0) in covered_hours:
                dropped += 1          # already represented at finer resolution
                continue
            kept.append((stamp, val))
        if dropped:
            log(f"    skipped {dropped} hourly rows already covered by 5-minute data")
        rows.extend(energy_to_rows(kept))

    log("  Month/Year sheets ignored on purpose (roll-ups of the same energy)")
    rows.sort(key=lambda r: r["recorded_at"])
    return rows


def gather_files(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(target.glob("*.xls"))
    if target.suffix.lower() == ".zip":
        tmp = Path(tempfile.mkdtemp(prefix="kasa_export_"))
        with zipfile.ZipFile(target) as z:
            z.extractall(tmp)
        return sorted(tmp.glob("*.xls"))
    return [target]


def main() -> int:
    ap = argparse.ArgumentParser(description="Import Kasa .xls exports into energy_readings")
    ap.add_argument("path", help="the .zip from the Kasa app, a folder, or an .xls file")
    ap.add_argument("--dry-run", action="store_true", help="show what would be written")
    args = ap.parse_args()

    for candidate in (Path(__file__).resolve().parents[2] / ".env", Path(".env")):
        if candidate.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(candidate)
                break
            except ImportError:
                pass

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    household = os.environ.get("KROVEN_HOUSEHOLD_ID", "").strip()
    if not (url and key and household):
        log("SUPABASE_URL, SUPABASE_SERVICE_KEY and KROVEN_HOUSEHOLD_ID must be set.")
        return 2

    target = Path(args.path)
    if not target.exists():
        log(f"No such path: {target}")
        return 2

    files = gather_files(target)
    if not files:
        log("No .xls files found.")
        return 2
    log(f"Files: {[f.name for f in files]}")

    rows = collect(files)
    if not rows:
        log("Nothing importable found.")
        return 0

    sink = SupabaseSink(url, key, household, log=log)
    cols = sink.detect_columns()

    existing = fetch_existing_timestamps(sink, household)
    log(f"\n  household already has {len(existing)} readings")

    payload, dupes, zeros = [], 0, 0
    for r in rows:
        iso = r["recorded_at"].isoformat()
        key = _key(r["recorded_at"])
        if key in existing:
            dupes += 1
            continue
        if r["kwh"] == 0:
            zeros += 1
        row = {
            "household_id": household,
            "recorded_at": iso,
            "kwh_consumed": round(r["kwh"], 6),
        }
        if "source" in cols:
            row["source"] = r["src"]
        if "watts" in cols and r["watts"] is not None:
            row["watts"] = r["watts"]
        if "device_name" in cols:
            row["device_name"] = os.environ.get("KASA_DEVICE_NAME", "PS5")
        payload.append(row)
        existing.add(key)      # guard against duplicates inside one file too

    total_kwh = sum(r["kwh_consumed"] for r in payload)
    log(f"  new rows: {len(payload)}   already present: {dupes}   of which zero-usage: {zeros}")
    if payload:
        log(f"  range: {payload[0]['recorded_at']} -> {payload[-1]['recorded_at']}")
        log(f"  total energy in this batch: {total_kwh:.3f} kWh")

    if args.dry_run:
        log("\n--dry-run: nothing written. Sample:")
        for row in payload[:3]:
            log(f"    {row}")
        return 0

    if not payload:
        log("\nNothing new to write.")
        return 0

    written = 0
    for i in range(0, len(payload), 500):
        chunk = payload[i:i + 500]
        if sink.write(chunk):
            written += len(chunk)
    log(f"\nWrote {written} readings. Household total now {sink.count_rows()}.")
    sink.close()
    return 0


def fetch_existing_timestamps(sink: SupabaseSink, household: str) -> set[str]:
    """Every recorded_at already stored for this household, for dedupe."""
    seen: set[str] = set()
    page = 0
    while True:
        try:
            r = sink._client.get(
                f"{sink.url}/rest/v1/energy_readings",
                params={"select": "recorded_at", "household_id": f"eq.{household}",
                        "limit": 1000, "offset": page * 1000},
            )
            batch = r.json()
        except Exception as e:
            log(f"[warn] could not read existing timestamps ({type(e).__name__}); "
                f"dedupe may be incomplete")
            return seen
        if not isinstance(batch, list) or not batch:
            return seen
        for row in batch:
            ts = row.get("recorded_at")
            if ts:
                seen.add(_key(ts))
        if len(batch) < 1000:
            return seen
        page += 1


def _key(value) -> str:
    """A comparable key for one instant.

    Must not be the raw isoformat string: we send '2026-08-23T00:00:00-07:00'
    and Postgres hands the same instant back as '2026-08-23T07:00:00+00:00'.
    Comparing strings therefore matched nothing and every re-run duplicated the
    whole file. Normalising both sides to UTC compares the actual moment.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
