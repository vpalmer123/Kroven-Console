"""
TP-Link Kasa adapter (built for a KP125M, works for any Kasa energy-monitoring plug).

Talks to the plug over the LAN — no cloud round trip. Newer Kasa firmware
(KP125M included: "Encrypt Type: TPAP, Login version: 2") authenticates with
your TP-Link account credentials even for local calls, so KASA_USERNAME and
KASA_PASSWORD must be set. They are read from the environment and never logged.
"""

from __future__ import annotations

import asyncio

from ..base import AdapterError, DeviceAdapter, Reading


class KasaPlugAdapter(DeviceAdapter):
    kind = "kasa"

    def __init__(self, device_name: str, host: str, username: str | None, password: str | None):
        super().__init__(device_name)
        self.host = host
        self._username = username
        self._password = password
        self._dev = None
        self._lock = asyncio.Lock()

    async def _connect(self):
        from kasa import Credentials, Discover

        creds = (
            Credentials(self._username, self._password)
            if self._username and self._password
            else None
        )
        try:
            dev = await Discover.discover_single(self.host, credentials=creds)
            await dev.update()
            return dev
        except Exception as e:
            # Some firmware advertises encrypt_type "TPAP", which python-kasa
            # does not recognise (upstream issue #1590) and so refuses before
            # it ever authenticates. A few of those units still accept a KLAP
            # handshake if you skip discovery and connect explicitly, so try
            # that before giving up. Needs credentials either way.
            if "TPAP" in str(e) or "Unsupported device" in str(e):
                forced = await self._connect_forced(creds)
                if forced is not None:
                    return forced
            raise AdapterError(f"could not connect to {self.host}: {type(e).__name__}: {e}") from e

    async def _connect_forced(self, creds):
        """Bypass discovery and dictate the transport. Returns None if it fails."""
        if creds is None:
            return None
        try:
            from kasa import Device, DeviceConfig
            from kasa.deviceconfig import (
                DeviceConnectionParameters,
                DeviceEncryptionType,
                DeviceFamily,
            )
        except Exception:
            return None

        families = (DeviceFamily.SmartKasaPlug, DeviceFamily.SmartTapoPlug)
        encryptions = (DeviceEncryptionType.Klap, DeviceEncryptionType.Aes)
        for family in families:
            for enc in encryptions:
                try:
                    conn = DeviceConnectionParameters(
                        device_family=family,
                        encryption_type=enc,
                        login_version=2,
                        https=False,
                    )
                    cfg = DeviceConfig(host=self.host, credentials=creds, connection_type=conn)
                    dev = await Device.connect(config=cfg)
                    await dev.update()
                    return dev
                except Exception:
                    continue
        return None

    async def _ensure(self):
        if self._dev is None:
            self._dev = await self._connect()
        return self._dev

    def _energy_module(self, dev):
        """python-kasa moved energy behind a module registry; older builds expose
        the values on the device. Try the modern path, then fall back."""
        try:
            from kasa import Module

            mod = dev.modules.get(Module.Energy)
            if mod is not None:
                return mod
        except Exception:
            pass
        return dev

    async def read(self) -> Reading:
        async with self._lock:
            dev = await self._ensure()
            try:
                await dev.update()
            except Exception as e:
                # Force a reconnect on the next poll — the plug may have
                # rebooted, changed IP, or dropped off the wifi.
                self._dev = None
                raise AdapterError(f"update failed for {self.host}: {type(e).__name__}: {e}") from e

            src = self._energy_module(dev)
            watts = _first_float(src, ("current_consumption", "power", "emeter_realtime_power"))
            kwh_today = _first_float(src, ("consumption_today", "energy_today", "emeter_today"))

            if watts is None and kwh_today is None:
                raise AdapterError(
                    f"{self.host} returned no energy data — is this an energy-monitoring model?"
                )

            extra = {"host": self.host}
            alias = getattr(dev, "alias", None)
            if alias:
                extra["device_alias"] = alias
            is_on = getattr(dev, "is_on", None)
            if is_on is not None:
                extra["is_on"] = bool(is_on)

            return Reading(
                device_name=self.device_name,
                source=self.source,
                taken_at=Reading.now(),
                watts=watts,
                kwh_today=kwh_today,
                extra=extra,
            )

    async def close(self) -> None:
        dev, self._dev = self._dev, None
        if dev is not None:
            try:
                await dev.disconnect()
            except Exception:
                pass


def _first_float(obj, names) -> float | None:
    for name in names:
        value = getattr(obj, name, None)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None
