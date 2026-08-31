"""
Reassign which physical device is on which load.

    python -m tools.swap_device_roles --dedicated shelly --aggregate kasa
    python -m tools.swap_device_roles --dedicated shelly --aggregate kasa --write

Hardware gets moved around. A plug that was on the PS5 ends up on a shared
cord and vice versa, which changes what its readings *mean*: a dedicated trace
is one appliance, an aggregate trace is a sum. The registry has to follow the
hardware or the forecaster learns from a series that describes nothing real.

The important part is what this does NOT do: it never touches a historical
reading. Instead it stamps each device with the role it used to hold and the
moment the role changed, so `signal_type_of()` can classify any row by the
role that was true when it was recorded. Rows logged before the swap keep
their original meaning; only new data is routed the new way.

Renaming is part of the swap — whatever is on the PS5 should be called PS5 —
and names are unique per household, so the rename is staged: every device is
moved to a temporary name first, then to its final one, otherwise the two
devices collide as they cross over.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _candidate in (Path(__file__).resolve().parents[1] / ".env", Path(".env")):
    if _candidate.exists():
        from dotenv import load_dotenv
        load_dotenv(_candidate)
        break

from app.db import get_db  # noqa: E402

# What each role looks like, independent of which hardware is filling it.
ROLES = {
    "dedicated": {
        "name": os.environ.get("ROLE_DEDICATED_NAME", "PS5"),
        "aliases": ["playstation", "play station", "console", "the ps5", "ps5 plug"],
        "controllable": True,
        "read_only_reason": None,
        "note": "single appliance behind this plug; trace is that appliance",
    },
    "aggregate": {
        "name": os.environ.get("ROLE_AGGREGATE_NAME", "extension cord"),
        "aliases": ["power strip", "the cord", "extension", "the strip"],
        "controllable": False,
        "read_only_reason": "shared circuit, monitored but not switchable",
        "note": "several loads behind one meter; trace is a sum",
    },
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Swap which device holds which role")
    ap.add_argument("--dedicated", required=True, help="device kind for the dedicated role")
    ap.add_argument("--aggregate", required=True, help="device kind for the aggregate role")
    ap.add_argument("--write", action="store_true", help="apply (default is a dry run)")
    args = ap.parse_args()

    household = os.environ.get("KROVEN_HOUSEHOLD_ID", "").strip()
    if not household:
        print("KROVEN_HOUSEHOLD_ID is not set.")
        return 2

    db = get_db()
    rows = db.table("devices").select("*").eq("household_id", household).execute().data or []
    if not rows:
        print("No devices registered.")
        return 2

    by_kind = {(r.get("kind") or "").lower(): r for r in rows}
    wanted = {"dedicated": args.dedicated.lower(), "aggregate": args.aggregate.lower()}

    for role, kind in wanted.items():
        if kind not in by_kind:
            print(f"No device of kind '{kind}' for the {role} role. "
                  f"Registered kinds: {', '.join(sorted(by_kind))}")
            return 2
    if wanted["dedicated"] == wanted["aggregate"]:
        print("One device cannot hold both roles.")
        return 2

    changed_at = datetime.now(timezone.utc).isoformat()
    plan = []

    for role, kind in wanted.items():
        row = by_kind[kind]
        spec = ROLES[role]
        old_role = row.get("signal_type") or "dedicated"
        meta = dict(row.get("meta") or {})

        # Only stamp a change when the role actually moved, so re-running does
        # not overwrite a real history with a no-op one.
        if old_role != role:
            meta["previous_signal_type"] = old_role
            meta["role_changed_at"] = changed_at
            # The name a reading carries is stronger evidence than its
            # timestamp: a logger mid-cycle can write one more row under the
            # old role after the database has already flipped.
            meta["previous_name"] = row.get("name")
            meta.setdefault("role_history", []).append(
                {"from": old_role, "to": role, "at": changed_at,
                 "previous_name": row.get("name")}
            )
        meta["aliases"] = spec["aliases"]
        meta["role_note"] = spec["note"]
        if spec["read_only_reason"]:
            meta["read_only_reason"] = spec["read_only_reason"]
        else:
            meta.pop("read_only_reason", None)

        plan.append({
            "id": row["id"],
            "kind": kind,
            "from_name": row.get("name"),
            "from_role": old_role,
            "to_name": spec["name"],
            "to_role": role,
            "controllable": spec["controllable"],
            "meta": meta,
        })

    for p in plan:
        moved = " (unchanged)" if p["from_role"] == p["to_role"] else ""
        print(f"  {p['kind']:<7} {p['from_name']!r} [{p['from_role']}] -> "
              f"{p['to_name']!r} [{p['to_role']}]{moved}")
        print(f"          controllable={p['controllable']}  "
              f"aliases={', '.join(p['meta']['aliases'])}")
        if p["meta"].get("role_changed_at"):
            print(f"          role_changed_at={p['meta']['role_changed_at']}")

    print("\n  Historical readings are NOT modified. Rows recorded before the")
    print("  change keep their original signal_type via role_changed_at.")

    if not args.write:
        print("\nDry run. Re-run with --write to apply.")
        return 0

    # Names are unique per household, so park everything on a temporary name
    # before assigning the final ones — otherwise the two devices collide
    # mid-swap when one takes a name the other still holds.
    try:
        for p in plan:
            db.table("devices").update(
                {"name": f"__swap__{p['kind']}"}
            ).eq("id", p["id"]).execute()

        for p in plan:
            db.table("devices").update({
                "name": p["to_name"],
                "signal_type": p["to_role"],
                "controllable": p["controllable"],
                "meta": p["meta"],
            }).eq("id", p["id"]).execute()
    except Exception as e:
        print(f"\nWrite failed ({type(e).__name__}): {e}")
        print("Devices may be left on temporary '__swap__' names — re-run to finish.")
        return 1

    print("\nApplied.")
    for r in db.table("devices").select("name,kind,signal_type,controllable,meta") \
               .eq("household_id", household).execute().data:
        hist = (r.get("meta") or {}).get("role_changed_at")
        print(f"  {r['name']:<16} {r['kind']:<7} {r['signal_type']:<10} "
              f"controllable={str(r['controllable']):<5} changed_at={hist}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
