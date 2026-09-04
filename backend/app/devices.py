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

    def __init__(self, message: str, needs_repair: bool = False):
        super().__init__(message)
        # Set when the stored credentials are half-written, so the caller can
        # flag the device for re-pairing rather than just reporting a failure
        # the user has no way to interpret.
        self.needs_repair = needs_repair


# The regional API host is an address, not a secret — it is printed next to the
# key in the Shelly app and identifies infrastructure, not an account. Falling
# back to a default therefore mixes nothing, which is why only the key and the
# device id are held to the all-or-nothing rule below.
DEFAULT_SHELLY_SERVER = "https://shelly-api-eu.shelly.cloud"


def _shelly_credentials(meta: dict, label: str, owner: bool) -> tuple[str, str, str]:
    """Resolve Shelly cloud credentials, all-or-nothing, from a single source.

    Three cases, and nothing between them:

      own      the device carries its own key and device id -> use only those,
               never touching the environment for any field
      legacy   the device carries none of the three -> the operator's own
               environment credentials, and only for the operator's household
      broken   the device carries some but not all -> refuse, and say the
               device needs reconnecting

    The middle case is the dangerous one and is why this exists. Resolving each
    field independently let them come from different accounts: a user's device
    with a key but no device id used to inherit the operator's SHELLY_DEVICE_ID
    and actuate the operator's plug.
    """
    server = (meta.get("cloud_server") or "").strip()
    key = (meta.get("cloud_auth_key") or "").strip()
    dev_id = (meta.get("cloud_device_id") or "").strip()

    if key and dev_id:
        # The device owns its credentials. A missing server is filled from the
        # default, never from the environment, so no account can leak into it.
        return (server or DEFAULT_SHELLY_SERVER), key, dev_id

    if key or dev_id or server:
        raise DeviceError(
            f"'{label}' is only half connected — its saved credentials are "
            f"incomplete. Reconnect it from Connect Device.",
            needs_repair=True,
        )

    if not owner:
        return "", "", ""

    env = (
        os.environ.get("SHELLY_CLOUD_SERVER", "").strip(),
        os.environ.get("SHELLY_CLOUD_AUTH_KEY", "").strip(),
        os.environ.get("SHELLY_DEVICE_ID", "").strip(),
    )
    # Complete set or nothing: a half-configured environment must not be
    # completed from anywhere else either.
    if env[1] and env[2]:
        return (env[0] or DEFAULT_SHELLY_SERVER), env[1], env[2]
    return "", "", ""


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
                  meta: dict | None = None, household_id: str | None = None):
    """Adapter for one registry row, chosen by `kind`.

    This is the only place a device type maps to a transport. control_device()
    stays free of per-device branching because everything type-specific is
    resolved here.
    """
    meta = meta or {}
    kind = (kind or "").strip().lower()
    owner = _is_owner_household(household_id)

    if kind == "shelly":
        # Credentials are taken as a COMPLETE SET from ONE source. They used to
        # fall back to the environment field by field, which let them mix
        # across accounts: a device row carrying a server and key but no
        # cloud_device_id fell through to the server operator's
        # SHELLY_DEVICE_ID, so one user's device could actuate the operator's
        # plug. Partial stored credentials are now an error, not something to
        # quietly complete from someone else's configuration.
        server, auth_key, device_id = _shelly_credentials(meta, label, owner)

        # Cloud wins when it is configured, even though LAN is faster.
        #
        # The deployed backend runs on Railway and the plug lives on a home
        # LAN, so a private address is simply unreachable from there — the
        # request times out and actuation fails, while working perfectly when
        # the same code runs on a laptop at home. Preferring LAN meant
        # configuring cloud credentials changed nothing, because the host was
        # always set and always won.
        #
        # Set SHELLY_PREFER_LOCAL=1 to invert this on a machine that really is
        # on the same network and wants the lower latency.
        prefer_local = os.environ.get("SHELLY_PREFER_LOCAL", "").strip().lower() in ("1", "true", "yes")
        has_cloud = bool(server and auth_key and device_id)

        if has_cloud and not prefer_local:
            return ShellyCloudAdapter(server, auth_key, device_id, channel, label)
        if host:
            return ShellyLocalAdapter(_as_url(host), channel, label)
        if has_cloud:
            return ShellyCloudAdapter(server, auth_key, device_id, channel, label)
        raise DeviceError(f"No address or cloud credentials configured for '{label}'.")

    if kind == "kasa":
        if not host:
            raise DeviceError(f"No address configured for '{label}'.")
        # Identical rule to Shelly, because it had the identical bug: the
        # operator's TP-Link login must never authenticate against a paired
        # user's hardware, and half a stored login must never be completed
        # from the environment.
        user = (meta.get("username") or "").strip()
        pw = (meta.get("password") or "").strip()

        if user and pw:
            pass                                    # device owns its login
        elif user or pw:
            raise DeviceError(
                f"'{label}' is only half connected — its saved credentials are "
                f"incomplete. Reconnect it from Connect Device.",
                needs_repair=True,
            )
        elif owner:
            env_user = os.environ.get("KASA_USERNAME", "").strip()
            env_pw = os.environ.get("KASA_PASSWORD", "").strip()
            user, pw = (env_user, env_pw) if (env_user and env_pw) else ("", "")

        return KasaLocalAdapter(host, channel, label, user, pw)

    raise DeviceError(f"Unknown device kind '{kind}' for '{label}'.")


def _is_owner_household(household_id: str | None) -> bool:
    """Whether this household is the one the server operator runs for itself.

    Environment credentials (SHELLY_CLOUD_*, KASA_*) are the operator's own
    account. They are a convenience for the household that owns the
    deployment, and must never stand in for a paired user's missing
    credentials — that is how one account's device ends up acting through
    another account's key.

    Defaults to False: an unknown or absent household gets no environment
    credentials at all.
    """
    owner = os.environ.get("KROVEN_HOUSEHOLD_ID", "").strip()
    return bool(owner and household_id and str(household_id).strip() == owner)


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
