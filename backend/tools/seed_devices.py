"""
Seed the `devices` registry from the env vars the logger already uses.

Run once after applying migrations/005_devices.sql:

    python -m tools.seed_devices            # show what would be written
    python -m tools.seed_devices --write    # actually write

Idempotent: matches on (household_id, name) and updates instead of duplicating,
so re-running after changing an address or an alias is safe.

Names and aliases come from configuration, never from literals here, so adding
a plug is a config + seed step rather than a code change.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must happen before app.db is imported: it reads SUPABASE_* into module-level
# constants at import time, so loading .env any later leaves them empty.
for _candidate in (Path(__file__).resolve().parents[1] / ".env", Path(".env")):
    if _candidate.exists():
        from dotenv import load_dotenv
        load_dotenv(_candidate)
        break

from app.db import get_db                      # noqa: E402
from app.device_registry import _env_devices   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the Kroven device registry")
    ap.add_argument("--write", action="store_true", help="write to Supabase")
    args = ap.parse_args()

    household = os.environ.get("KROVEN_HOUSEHOLD_ID", "").strip()
    if not household:
        print("KROVEN_HOUSEHOLD_ID is not set; nothing to attach devices to.")
        return 2

    devices = _env_devices(household)
    if not devices:
        print("No devices configured. Set KASA_HOST / SHELLY_HOST first.")
        return 2

    rows = []
    for d in devices:
        rows.append({
            "household_id": household,
            "name": d["name"],
            "kind": d["kind"],
            "host": d["host"],
            "channel": d["channel"],
            "signal_type": d["signal_type"],
            "controllable": d["controllable"],
            "state": "unknown",
            "meta": d.get("meta") or {},
        })

    for r in rows:
        print(f"  {r['name']:<16} {r['kind']:<7} {r['signal_type']:<10} "
              f"controllable={str(r['controllable']):<5} host={r['host']}")
        aliases = (r["meta"] or {}).get("aliases") or []
        if aliases:
            print(f"      also called: {', '.join(aliases)}")

    if not args.write:
        print("\nDry run. Re-run with --write to apply.")
        return 0

    db = get_db()
    try:
        db.table("devices").upsert(rows, on_conflict="household_id,name").execute()
    except Exception as e:
        print(f"\nWrite failed ({type(e).__name__}): {e}")
        print("Has migrations/005_devices.sql been applied?")
        return 1

    print(f"\nWrote {len(rows)} device(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
