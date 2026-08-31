"""
Decide whether a Kasa plug can actually be driven locally.

    python -m tools.probe_kasa                    # probe KASA_HOST from .env
    python -m tools.probe_kasa --host 10.0.0.55   # probe a specific address
    python -m tools.probe_kasa --discover         # sweep the LAN for plugs
    python -m tools.probe_kasa --host X --switch  # also cycle it off/on

Written because "it's a different model, it should work" is not evidence. The
KP125M advertises TP-Link's TPAP scheme, which no released python-kasa
implements, so it fails before credentials are ever tried — and the error it
raises (TRANSPORT_UNKNOWN_CREDENTIALS_ERROR 1003) reads exactly like a wrong
password, which sends you rotating credentials for a problem that is not about
credentials. This script separates the two cases and says which one you have.

The verdict is one of:

  SUPPORTED    connects and reads. Register it and use it.
  TPAP-BLOCKED firmware speaks a protocol python-kasa cannot. Not fixable
               in config; the device needs replacing or library support.
  AUTH-FAILED  protocol is fine, credentials are wrong. Fix KASA_USERNAME /
               KASA_PASSWORD.
  UNREACHABLE  nothing answered. Wrong address, or not on this network.

--switch is opt-in and never implied: it cuts real power to whatever is
plugged in, and confirms the result by reading the device back.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _candidate in (Path(__file__).resolve().parents[1] / ".env", Path(".env")):
    if _candidate.exists():
        from dotenv import load_dotenv
        load_dotenv(_candidate)
        break


async def probe(host: str, user: str, pw: str) -> tuple[str, str]:
    """Returns (verdict, detail)."""
    from kasa import Credentials, Discover

    creds = Credentials(user, pw) if user else None

    # 1. The normal path. If this works, nothing else matters.
    try:
        dev = await Discover.discover_single(host, credentials=creds, timeout=8)
        await dev.update()
        energy = dev.modules.get("Energy")
        watts = getattr(energy, "current_consumption", None) if energy else None
        detail = (f"{dev.model} '{dev.alias}' | on={dev.is_on} | "
                  f"power={watts} W | transport={type(dev.protocol._transport).__name__}")
        await dev.disconnect()
        return "SUPPORTED", detail
    except Exception as e:
        first = f"{type(e).__name__}: {str(e)[:160]}"
        if "Unsupported" in type(e).__name__ or "TPAP" in str(e):
            # Confirm by forcing every transport, so the verdict is not one
            # library heuristic's opinion.
            return await _force_matrix(host, creds, first)
        if "Auth" in type(e).__name__:
            return "AUTH-FAILED", first
        if "Timeout" in type(e).__name__ or "Connection" in type(e).__name__:
            return "UNREACHABLE", first
        return await _force_matrix(host, creds, first)


async def _force_matrix(host: str, creds, first: str) -> tuple[str, str]:
    """Bypass discovery's own scheme check and try each transport directly."""
    from kasa.device import Device
    from kasa.deviceconfig import (DeviceConfig, DeviceConnectionParameters,
                                   DeviceEncryptionType, DeviceFamily)

    families = [DeviceFamily.SmartKasaPlug, DeviceFamily.SmartTapoPlug,
                DeviceFamily.IotSmartPlugSwitch]
    encs = [DeviceEncryptionType.Aes, DeviceEncryptionType.Klap,
            DeviceEncryptionType.Xor]

    tried, saw_auth = 0, False
    for fam, enc, https in itertools.product(families, encs, (False, True)):
        tried += 1
        try:
            conn = DeviceConnectionParameters(device_family=fam, encryption_type=enc,
                                              login_version=2, https=https)
            cfg = DeviceConfig(host=host, credentials=creds, connection_type=conn,
                               timeout=6)
            dev = await Device.connect(config=cfg)
            await dev.update()
            detail = f"{fam.value}/{enc.value}/https={https} -> {dev.model} on={dev.is_on}"
            await dev.disconnect()
            return "SUPPORTED", detail
        except Exception as e:
            if "Auth" in type(e).__name__:
                saw_auth = True

    if saw_auth:
        # AES completed its handshake and the device rejected the login. On a
        # TPAP device this happens for every model and is NOT a password fault,
        # so it is reported as blocked rather than as an auth failure.
        return ("TPAP-BLOCKED",
                f"all {tried} transport combinations failed; AES reached the device and "
                f"was rejected at credential exchange. First error: {first}")
    return "TPAP-BLOCKED", f"all {tried} transport combinations failed. First error: {first}"


async def cycle(host: str, user: str, pw: str) -> None:
    """Switch off, confirm at the device, switch back on, confirm again."""
    from kasa import Credentials, Discover

    creds = Credentials(user, pw) if user else None

    async def read():
        d = await Discover.discover_single(host, credentials=creds, timeout=8)
        await d.update()
        energy = d.modules.get("Energy")
        state = (d.is_on, getattr(energy, "current_consumption", None) if energy else None)
        await d.disconnect()
        return state

    on, w = await read()
    print(f"  before      on={on} power={w} W")

    for want, label in ((False, "off"), (True, "on")):
        d = await Discover.discover_single(host, credentials=creds, timeout=8)
        await d.update()
        await (d.turn_on() if want else d.turn_off())
        await d.disconnect()
        await asyncio.sleep(2.0)
        on, w = await read()
        ok = "OK " if on is want else "FAIL"
        print(f"  {label:<11} on={on} power={w} W   [{ok}]")


async def main_async(args) -> int:
    user = os.environ.get("KASA_USERNAME", "")
    pw = os.environ.get("KASA_PASSWORD", "")

    if args.discover:
        from kasa import Credentials, Discover
        print("Sweeping the LAN (6s)...")
        found = await Discover.discover(
            credentials=Credentials(user, pw) if user else None, timeout=6)
        if not found:
            print("  no Kasa devices answered.")
            return 0
        for ip, d in found.items():
            print(f"  {ip:<16} {d.model}")
        return 0

    host = args.host or os.environ.get("KASA_HOST", "").strip()
    if not host:
        print("No host. Pass --host or set KASA_HOST.")
        return 2

    print(f"Probing {host} ...\n")
    verdict, detail = await probe(host, user, pw)
    print(f"  VERDICT: {verdict}")
    print(f"  {detail}\n")

    if verdict == "SUPPORTED":
        print("  Register it:  add KASA_HOST / KASA_DEVICE_NAME to .env, then")
        print("                python -m tools.seed_devices --write")
        if args.switch:
            print("\n  Cycling power (this really switches the load):")
            await cycle(host, user, pw)
    elif verdict == "TPAP-BLOCKED":
        print("  Not fixable in config. This needs different hardware (KP115 /")
        print("  HS103 use the older protocol) or python-kasa TPAP support.")
    elif verdict == "AUTH-FAILED":
        print("  Protocol is fine. Check KASA_USERNAME / KASA_PASSWORD.")
    else:
        print("  Check the address, and that this machine is on the same network.")

    return 0 if verdict == "SUPPORTED" else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe a Kasa plug for local control")
    ap.add_argument("--host", help="device address (default: KASA_HOST)")
    ap.add_argument("--discover", action="store_true", help="sweep the LAN instead")
    ap.add_argument("--switch", action="store_true",
                    help="if supported, cycle power off then on to prove actuation")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
