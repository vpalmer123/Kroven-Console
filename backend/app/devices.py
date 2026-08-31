"""
Device actuation layer — this is what turns Kroven from an advisor into an
actuator. Everything here drives real hardware; there is no simulation path.

Two transports, because they solve different deployment shapes:

  cloud  — Shelly Cloud API. Required when the backend runs on Railway,
           because Railway cannot reach a plug sitting on a home LAN.
           Get the server + auth key from the Shelly app:
           Settings -> Authorization cloud key.

  local  — direct HTTP to the plug's IP. Only works when the backend runs
           on the same network as the plug (e.g. laptop during a demo).
           Supports both Gen1 (/relay/0) and Gen2+ RPC (/rpc/Switch.Set).

If no transport is configured, get_adapter() returns None and the rest of
the app treats device control as unavailable. It never pretends otherwise —
callers are expected to surface "no device paired" rather than fake a toggle.

Env vars:
    SHELLY_CLOUD_SERVER    e.g. shelly-53-eu.shelly.cloud
    SHELLY_CLOUD_AUTH_KEY  auth key from the Shelly app
    SHELLY_DEVICE_ID       device id from the Shelly app
    SHELLY_LOCAL_URL       e.g. http://192.168.1.42  (alternative to cloud)
    SHELLY_CHANNEL         relay/switch channel, default 0
    SHELLY_DEVICE_LABEL    human name used in explanations, default "smart plug"
"""

import os
from typing import Any

import httpx

TIMEOUT = 10.0


class DeviceError(RuntimeError):
    """Raised when the physical device could not be read or actuated."""


class ShellyCloudAdapter:
    transport = "cloud"

    def __init__(self, server: str, auth_key: str, device_id: str, channel: int, label: str):
        server = server.strip().replace("https://", "").replace("http://", "").rstrip("/")
        self.base = f"https://{server}"
        self.auth_key = auth_key
        self.device_id = device_id
        self.channel = channel
        self.label = label

    async def _post(self, path: str, extra: dict[str, str] | None = None) -> dict[str, Any]:
        data = {"id": self.device_id, "auth_key": self.auth_key}
        if extra:
            data.update(extra)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(f"{self.base}{path}", data=data)
        if r.status_code != 200:
            raise DeviceError(f"Shelly cloud returned HTTP {r.status_code}: {r.text[:200]}")
        payload = r.json()
        if not payload.get("isok", False):
            raise DeviceError(f"Shelly cloud rejected the request: {payload.get('errors') or payload}")
        return payload

    async def get_state(self) -> dict[str, Any]:
        payload = await self._post("/device/status")
        status = (payload.get("data") or {}).get("device_status") or {}
        return _normalize_state(status, self.channel, self.label, self.transport)

    async def set_switch(self, on: bool) -> dict[str, Any]:
        await self._post(
            "/device/relay/control",
            {"channel": str(self.channel), "turn": "on" if on else "off"},
        )
        return await self.get_state()


class ShellyLocalAdapter:
    transport = "local"

    def __init__(self, base_url: str, channel: int, label: str):
        self.base = base_url.rstrip("/")
        self.channel = channel
        self.label = label
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

    async def get_state(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            gen = await self._detect_gen(client)
            try:
                if gen >= 2:
                    r = await client.get(f"{self.base}/rpc/Switch.GetStatus", params={"id": self.channel})
                    raw = r.json()
                    status = {"switch:%d" % self.channel: raw}
                else:
                    r = await client.get(f"{self.base}/status")
                    status = r.json()
            except httpx.HTTPError as e:
                raise DeviceError(f"Could not reach the plug at {self.base}: {e}") from e
        if r.status_code != 200:
            raise DeviceError(f"Plug returned HTTP {r.status_code}")
        return _normalize_state(status, self.channel, self.label, self.transport)

    async def set_switch(self, on: bool) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            gen = await self._detect_gen(client)
            try:
                if gen >= 2:
                    r = await client.get(
                        f"{self.base}/rpc/Switch.Set",
                        params={"id": self.channel, "on": "true" if on else "false"},
                    )
                else:
                    r = await client.get(
                        f"{self.base}/relay/{self.channel}",
                        params={"turn": "on" if on else "off"},
                    )
            except httpx.HTTPError as e:
                raise DeviceError(f"Could not reach the plug at {self.base}: {e}") from e
        if r.status_code != 200:
            raise DeviceError(f"Plug returned HTTP {r.status_code} on switch command")
        return await self.get_state()


def _normalize_state(status: dict, channel: int, label: str, transport: str) -> dict[str, Any]:
    """Flatten Gen1 and Gen2 status payloads into one shape."""
    on: bool | None = None
    power_w: float | None = None
    energy_wh: float | None = None

    gen2_key = f"switch:{channel}"
    if gen2_key in status and isinstance(status[gen2_key], dict):
        sw = status[gen2_key]
        on = sw.get("output")
        power_w = sw.get("apower")
        energy_wh = (sw.get("aenergy") or {}).get("total")
    else:
        relays = status.get("relays") or []
        meters = status.get("meters") or []
        if channel < len(relays):
            on = relays[channel].get("ison")
        if channel < len(meters):
            power_w = meters[channel].get("power")
            energy_wh = meters[channel].get("total")

    if on is None:
        raise DeviceError("Device responded but reported no switch state for this channel.")

    return {
        "label": label,
        "transport": transport,
        "channel": channel,
        "on": bool(on),
        "power_w": power_w,
        "energy_wh": energy_wh,
        "online": True,
    }


class KasaLocalAdapter:
    """TP-Link Kasa plug over the local protocol.

    Kept deliberately thin: python-kasa owns the protocol negotiation, and the
    only thing worth adding is a legible failure. The KP125M speaks TP-Link's
    TPAP scheme, which no released python-kasa (0.10.2 is current) implements,
    so connect() raises UnsupportedDeviceError before credentials are even
    tried. That is a firmware/library gap, not a misconfiguration, and the
    error text says so — otherwise it reads as a password problem forever.
    """

    transport = "local"
    kind = "kasa"

    def __init__(self, host: str, channel: int, label: str,
                 username: str = "", password: str = ""):
        self.host = host.replace("http://", "").replace("https://", "").strip("/")
        self.channel = channel
        self.label = label
        self.username = username
        self.password = password

    async def _connect(self):
        try:
            from kasa import Credentials, Discover
        except ImportError as e:  # pragma: no cover - dependency is declared
            raise DeviceError("python-kasa is not installed on this host.") from e

        creds = Credentials(self.username, self.password) if self.username else None
        try:
            dev = await Discover.discover_single(self.host, credentials=creds, timeout=8)
            await dev.update()
            return dev
        except Exception as e:
            name = type(e).__name__
            if "Unsupported" in name or "TPAP" in str(e):
                raise DeviceError(
                    f"{self.label} at {self.host} uses TP-Link's TPAP encryption, which "
                    f"python-kasa cannot speak yet, so it can be NEITHER read NOR "
                    f"switched — the connection fails before any command is sent. "
                    f"This is a protocol gap, not a credential problem."
                ) from e
            if "Auth" in name:
                raise DeviceError(
                    f"{self.label} rejected the stored TP-Link credentials ({name})."
                ) from e
            raise DeviceError(f"Could not reach {self.label} at {self.host}: {name}: {e}") from e

    async def get_state(self) -> dict[str, Any]:
        dev = await self._connect()
        try:
            energy = dev.modules.get("Energy")
            return {
                "label": self.label,
                "transport": self.transport,
                "channel": self.channel,
                "on": bool(dev.is_on),
                "power_w": getattr(energy, "current_consumption", None) if energy else None,
                "energy_wh": None,
                "online": True,
            }
        finally:
            await dev.disconnect()

    async def set_switch(self, on: bool) -> dict[str, Any]:
        dev = await self._connect()
        try:
            await (dev.turn_on() if on else dev.turn_off())
            await dev.update()
            energy = dev.modules.get("Energy")
            return {
                "label": self.label,
                "transport": self.transport,
                "channel": self.channel,
                "on": bool(dev.is_on),
                "power_w": getattr(energy, "current_consumption", None) if energy else None,
                "energy_wh": None,
                "online": True,
            }
        finally:
            await dev.disconnect()


def build_adapter(kind: str, host: str, channel: int = 0, label: str = "plug",
                  meta: dict | None = None):
    """Adapter for one registry row, chosen by `kind`.

    This is the only place a device type maps to a transport. control_device()
    stays free of per-device branching because everything type-specific is
    resolved here.
    """
    meta = meta or {}
    kind = (kind or "").strip().lower()

    if kind == "shelly":
        server = meta.get("cloud_server") or os.environ.get("SHELLY_CLOUD_SERVER", "").strip()
        auth_key = meta.get("cloud_auth_key") or os.environ.get("SHELLY_CLOUD_AUTH_KEY", "").strip()
        device_id = meta.get("cloud_device_id") or os.environ.get("SHELLY_DEVICE_ID", "").strip()
        # Prefer LAN when we have an address: it is faster and keeps working
        # when the cloud is down. Cloud is the fallback for off-network hosts.
        if host:
            return ShellyLocalAdapter(_as_url(host), channel, label)
        if server and auth_key and device_id:
            return ShellyCloudAdapter(server, auth_key, device_id, channel, label)
        raise DeviceError(f"No address or cloud credentials configured for '{label}'.")

    if kind == "kasa":
        if not host:
            raise DeviceError(f"No address configured for '{label}'.")
        return KasaLocalAdapter(
            host, channel, label,
            meta.get("username") or os.environ.get("KASA_USERNAME", ""),
            meta.get("password") or os.environ.get("KASA_PASSWORD", ""),
        )

    raise DeviceError(f"Unknown device kind '{kind}' for '{label}'.")


def _as_url(host: str) -> str:
    host = host.strip().rstrip("/")
    return host if host.startswith(("http://", "https://")) else f"http://{host}"


def get_adapter() -> ShellyCloudAdapter | ShellyLocalAdapter | None:
    """Return a configured adapter, or None when no device is paired."""
    channel = int(os.environ.get("SHELLY_CHANNEL", "0"))
    label = os.environ.get("SHELLY_DEVICE_LABEL", "smart plug")

    server = os.environ.get("SHELLY_CLOUD_SERVER", "").strip()
    auth_key = os.environ.get("SHELLY_CLOUD_AUTH_KEY", "").strip()
    device_id = os.environ.get("SHELLY_DEVICE_ID", "").strip()
    if server and auth_key and device_id:
        return ShellyCloudAdapter(server, auth_key, device_id, channel, label)

    local_url = os.environ.get("SHELLY_LOCAL_URL", "").strip()
    if local_url:
        return ShellyLocalAdapter(local_url, channel, label)

    return None


def is_configured() -> bool:
    return get_adapter() is not None
