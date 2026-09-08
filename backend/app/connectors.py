"""How a device gets connected — by method, not by brand.

Kroven does not publish a catalogue of supported manufacturers, and the user is
never asked to pick one. They are asked *how* they want to connect: a URL/API
endpoint, or a direct local connection. Kroven then works out for itself what
is on the other end.

WHY THIS WAY ROUND
A brand list is a promise about a product range, and it is the wrong promise
here. Shelly is the implementation behind one connected device, not a Kroven
product option — presenting it as a menu item tells every new user something
false about their own home before they have connected anything at all. It also
ages badly: every device Kroven learns to speak to would have to become another
tile, and every device it cannot would look deliberately excluded.

Asking for the connection method instead is both smaller and more honest. The
user knows whether they have an endpoint and a key or a box on their own
network. They should not have to know what firmware is inside it.

    identify(method, endpoint, credential) -> (connector, [Discovered, ...])

Each connector claims an endpoint or declines it. Declining raises
NotThisDevice and the next connector is tried; recognising the endpoint but
failing to authenticate raises ConnectorError and stops the search, because
"that key was rejected" is a far more useful answer than "nothing recognised
this". Adding support for new hardware means adding a connector to CONNECTORS,
and changes no user-facing text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("kroven.connectors")

TIMEOUT = 20


class NotThisDevice(Exception):
    """This connector does not recognise the endpoint. Try the next one."""


class ConnectorError(Exception):
    """The endpoint was recognised, but the connection could not be made."""


@dataclass
class Discovered:
    """One device, normalised. Nothing brand-specific survives past here."""

    external_id: str
    kind: str                       # internal adapter key, never shown as a catalogue
    provider_name: str | None       # exactly what the device called itself, or None
    switchable: bool
    channel: int = 0
    model: str | None = None
    gen: Any = None
    online: bool = False
    category: str | None = None
    host: str | None = None
    transport: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# the methods a user chooses between
# ---------------------------------------------------------------------------
# Deliberately generic, and deliberately short. These are ways of reaching a
# device, not products. No manufacturer, model or firmware appears here.
METHODS = [
    {
        "id": "url_api",
        "name": "URL / API",
        "help": "Connect using a device or API endpoint.",
        "fields": [
            {"id": "endpoint", "label": "Endpoint URL", "required": True},
            {"id": "credential", "label": "API key or token", "required": False,
             "secret": True},
        ],
    },
    {
        "id": "local",
        "name": "Local / Direct",
        "help": "Connect using an available local connection method.",
        "fields": [
            {"id": "endpoint", "label": "Host or IP address", "required": True},
            {"id": "credential", "label": "Password, if the device has one",
             "required": False, "secret": True},
        ],
    },
]

METHOD_IDS = tuple(m["id"] for m in METHODS)


def normalise_endpoint(raw: str, method: str) -> str:
    s = (raw or "").strip().rstrip("/")
    if not s:
        return s
    if not s.startswith(("http://", "https://")):
        # A bare host on the local network is almost never TLS; a bare hostname
        # given as an API endpoint almost always is.
        s = ("http://" if method == "local" else "https://") + s
    return s


# ---------------------------------------------------------------------------
# connectors
# ---------------------------------------------------------------------------

class CloudRelayConnector:
    """A hosted account API that lists relay devices and their ids.

    Recognition is by response shape, not by hostname, so this claims any
    endpoint that answers the account-listing call in the expected form.
    """

    id = "cloud_relay"
    kind = "shelly"
    methods = ("url_api",)
    path = "/interface/device/list"

    async def discover(self, endpoint: str, credential: str) -> list[Discovered]:
        if not credential:
            raise NotThisDevice("no credential supplied")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.post(f"{endpoint}{self.path}",
                                 data={"auth_key": credential})
        except httpx.HTTPError as e:
            raise NotThisDevice(f"endpoint unreachable: {type(e).__name__}") from e

        if r.status_code in (401, 403):
            # Recognised, but refused. Say so rather than falling through to
            # "we couldn't identify this", which would send the user hunting
            # for the wrong problem.
            raise ConnectorError(
                "That endpoint rejected the key. Check it was copied in full, "
                "and that the address matches the one issued with it."
            )
        if r.status_code == 404:
            raise NotThisDevice("no account listing at this endpoint")
        if r.status_code != 200:
            raise NotThisDevice(f"unexpected status {r.status_code}")

        try:
            payload = r.json()
        except ValueError as e:
            raise NotThisDevice("endpoint did not return JSON") from e

        if not isinstance(payload, dict) or "isok" not in payload:
            raise NotThisDevice("not an account listing")
        if not payload.get("isok"):
            raise ConnectorError("That endpoint rejected the key.")

        listing = ((payload.get("data") or {}).get("devices") or {})
        if not isinstance(listing, dict):
            raise ConnectorError("The endpoint answered, but not with a device list.")

        found = []
        for dev_id, d in listing.items():
            d = d or {}
            raw_name = (d.get("name") or "").strip()
            found.append(Discovered(
                external_id=dev_id,
                kind=self.kind,
                provider_name=raw_name or None,
                # Only something with a relay can be switched. Anything else
                # would show a control that does nothing.
                switchable=(d.get("category") == "relay") or d.get("mode") == "relay",
                channel=int(d.get("channel") or 0),
                model=d.get("type"),
                gen=d.get("gen"),
                online=bool(d.get("cloud_online")),
                category=d.get("category"),
                transport={"cloud_server": endpoint, "cloud_auth_key": credential,
                           "cloud_device_id": dev_id, "paired_via": "cloud_api"},
            ))
        return found


class LocalHttpConnector:
    """A device on the user's own network that describes itself over HTTP.

    One unauthenticated identity call. If the answer names a firmware and a
    model, this is a device Kroven can drive directly.
    """

    id = "local_http"
    kind = "shelly"
    methods = ("local",)

    async def discover(self, endpoint: str, credential: str) -> list[Discovered]:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.get(f"{endpoint}/shelly")
        except httpx.HTTPError as e:
            # From a hosted backend this is the normal outcome for a home IP,
            # and the caller turns it into a reachability message rather than
            # an identification failure.
            raise NotThisDevice(f"host unreachable: {type(e).__name__}") from e

        if r.status_code != 200:
            raise NotThisDevice(f"unexpected status {r.status_code}")
        try:
            info = r.json()
        except ValueError as e:
            raise NotThisDevice("host did not return JSON") from e
        if not isinstance(info, dict) or not (info.get("type") or info.get("model")):
            raise NotThisDevice("host did not identify itself")

        ident = (info.get("mac") or info.get("id") or endpoint).strip()
        gen = info.get("gen", 1)
        return [Discovered(
            external_id=ident,
            kind=self.kind,
            provider_name=(info.get("name") or "").strip() or None,
            # A relay count of zero means there is nothing to switch. Absent,
            # assume there is one, which is what a single-channel unit reports.
            switchable=int(info.get("num_outputs") or info.get("num_relays") or 1) > 0,
            model=info.get("model") or info.get("type"),
            gen=gen,
            online=True,
            host=endpoint,
            transport={"local_host": endpoint, "paired_via": "local_http",
                       "local_password": credential or None},
        )]


CONNECTORS: list[Any] = [CloudRelayConnector(), LocalHttpConnector()]


async def identify(method: str, endpoint: str, credential: str):
    """Find whatever is at this endpoint, or explain why nothing was.

    Returns (connector, devices). Raises ConnectorError with a message meant
    for the person who typed the address.
    """
    candidates = [c for c in CONNECTORS if method in c.methods]
    if not candidates:
        raise ConnectorError("That connection method isn't available yet.")

    declined = []
    for c in candidates:
        try:
            devices = await c.discover(endpoint, credential)
        except NotThisDevice as e:
            declined.append(f"{c.id}: {e}")
            continue
        if devices:
            return c, devices
        raise ConnectorError("Kroven reached that endpoint, but it reported no devices.")

    logger.info("no connector claimed %s endpoint (%s)", method, "; ".join(declined))
    if method == "local":
        raise ConnectorError(
            "Kroven couldn't reach that address. A device on your home network "
            "isn't reachable from Kroven's servers unless you've forwarded it, "
            "so a URL/API endpoint is usually the way in."
        )
    raise ConnectorError(
        "Kroven couldn't identify a device at that address. Check the endpoint, "
        "and that any key it needs was included."
    )
