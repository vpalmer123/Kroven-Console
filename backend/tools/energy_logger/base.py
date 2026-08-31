"""
Common shape every device adapter speaks.

The polling loop and the database sink only ever see a Reading, so adding a
device type never touches either of them. That matters because the third
planned source (ESP32 WiFi CSI) is not a wattage meter at all — hence `watts`
and `kwh_today` are optional and `extra` carries whatever a given device
actually produces.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class Reading:
    """One sample from one device."""

    device_name: str                  # human label, e.g. "PS5"
    source: str                       # adapter id + device, e.g. "kasa:PS5"
    taken_at: datetime
    watts: float | None = None        # instantaneous draw
    kwh_today: float | None = None    # device's own cumulative-today counter
    extra: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)


class AdapterError(RuntimeError):
    """Raised for an expected, retryable device failure."""


class DeviceAdapter(abc.ABC):
    """A pollable device.

    Implementations must be safe to call repeatedly for hours: connect lazily,
    tolerate the device disappearing, and raise AdapterError rather than
    leaking transport-specific exceptions.
    """

    #: short id used in the `source` field and in config, e.g. "kasa"
    kind: str = "device"

    def __init__(self, device_name: str):
        self.device_name = device_name

    @property
    def source(self) -> str:
        return f"{self.kind}:{self.device_name}"

    @abc.abstractmethod
    async def read(self) -> Reading:
        """Return one sample, or raise AdapterError if the device is unreachable."""

    async def close(self) -> None:
        """Release any held connection. Safe to call more than once."""
        return None

    def describe(self) -> str:
        return f"{self.kind} adapter '{self.device_name}'"
