"""
Adapter registry.

Adding a device type means writing one class and adding one line to REGISTRY —
main.py and sink.py never change. The ESP32 CSI source will slot in here the
same way, even though it produces something other than watts.
"""

from __future__ import annotations

import os

from ..base import DeviceAdapter
from .esp32_csi import Esp32CsiAdapter, from_env as esp32_from_env
from .kasa_plug import KasaPlugAdapter
from .shelly_plug import ShellyPlugAdapter

REGISTRY: dict[str, type[DeviceAdapter]] = {
    KasaPlugAdapter.kind: KasaPlugAdapter,
    ShellyPlugAdapter.kind: ShellyPlugAdapter,
    Esp32CsiAdapter.kind: Esp32CsiAdapter,
}


def build_adapters() -> tuple[list[DeviceAdapter], list[str]]:
    """Construct every adapter the environment has configured.

    Returns (adapters, notes). A device with no host configured is skipped with
    a note rather than an error — that is how the Shelly stays dormant until
    SHELLY_HOST is set.
    """
    adapters: list[DeviceAdapter] = []
    notes: list[str] = []

    kasa_host = os.environ.get("KASA_HOST", "").strip()
    if kasa_host:
        user = os.environ.get("KASA_USERNAME", "").strip() or None
        pw = os.environ.get("KASA_PASSWORD", "").strip() or None
        if not (user and pw):
            notes.append(
                "KASA_HOST is set but KASA_USERNAME/KASA_PASSWORD are not. Newer Kasa "
                "firmware (KP125M included) requires account credentials even for LAN "
                "access, so this will likely fail to authenticate."
            )
        adapters.append(
            KasaPlugAdapter(
                device_name=os.environ.get("KASA_DEVICE_NAME", "PS5").strip() or "PS5",
                host=kasa_host,
                username=user,
                password=pw,
            )
        )
    else:
        notes.append("KASA_HOST not set — Kasa adapter inactive.")

    shelly_host = os.environ.get("SHELLY_HOST", "").strip()
    if shelly_host:
        adapters.append(
            ShellyPlugAdapter(
                device_name=os.environ.get("SHELLY_DEVICE_NAME", "shelly").strip() or "shelly",
                host=shelly_host,
                channel=int(os.environ.get("SHELLY_CHANNEL", "0")),
            )
        )
    else:
        notes.append("SHELLY_HOST not set — Shelly adapter inactive (set it when the plug arrives).")

    esp32 = esp32_from_env()
    if esp32 is not None:
        adapters.append(esp32)
    else:
        notes.append("ESP32_CSI_PORT/ESP32_CSI_UDP not set — CSI adapter inactive (no board yet).")

    return adapters, notes
