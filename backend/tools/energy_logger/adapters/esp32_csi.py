"""
ESP32 WiFi CSI adapter — built ahead of the hardware, inert until configured.

WHAT CSI IS, AND WHY IT DOESN'T FIT THE PLUG SHAPE
--------------------------------------------------
Channel State Information is the per-subcarrier amplitude and phase of a WiFi
link. People moving through the space perturb it, so CSI is a motion/occupancy
sensor, not a power meter. It produces no watts and no kWh — which is exactly
why `Reading.watts` and `Reading.kwh_today` are optional and `Reading.extra`
exists. This adapter fills `extra` and leaves the energy fields as None, and
the sink already declines to write a row with no energy in it.

That is deliberate: CSI feeds the HAR labeller (see har.py), not the kWh series.

FIRMWARE — DON'T WRITE YOUR OWN
-------------------------------
Use an existing toolkit. The established one is ESP32-CSI-Tool
(https://github.com/StevenMHernandez/ESP32-CSI-Tool), which flashes an
active/passive sketch and emits CSI as CSV over serial or UDP. Alternatives
worth a look: ESP-CSI (Espressif's own, https://github.com/espressif/esp-csi)
and Nexmon CSI for Broadcom radios.

Both transports the toolkit offers are handled here:

  serial  ESP32_CSI_PORT=COM5           reads CSV lines over USB
  udp     ESP32_CSI_UDP=0.0.0.0:5566    listens for CSV datagrams

The CSV line layout differs between toolkit versions, so parsing is defensive:
we locate the bracketed subcarrier vector and the RSSI/MAC fields by shape
rather than by fixed column index, and anything unrecognised is skipped rather
than guessed at.

STATUS: parsing and transport are implemented; nothing has been validated
against real hardware, because there is no board yet. Treat the first live
capture as a bring-up test, not a working sensor.
"""

from __future__ import annotations

import asyncio
import os
import re
import statistics
from collections import deque

from ..base import AdapterError, DeviceAdapter, Reading

VECTOR_RE = re.compile(r"\[([-0-9,\s]+)\]")


class Esp32CsiAdapter(DeviceAdapter):
    kind = "esp32csi"

    def __init__(self, device_name: str, port: str | None = None, udp: str | None = None,
                 window: int = 64):
        super().__init__(device_name)
        self.port = port
        self.udp = udp
        self.window = window
        self._recent: deque[float] = deque(maxlen=window)
        self._serial = None
        self._sock = None

    # ---------- transports ----------

    def _open_serial(self):
        if self._serial is not None:
            return self._serial
        try:
            import serial  # pyserial, optional until the board exists
        except ImportError as e:
            raise AdapterError(
                "pyserial is not installed. `pip install pyserial` when the board arrives."
            ) from e
        try:
            self._serial = serial.Serial(self.port, baudrate=921600, timeout=1)
        except Exception as e:
            raise AdapterError(f"could not open {self.port}: {type(e).__name__}: {e}") from e
        return self._serial

    def _open_udp(self):
        if self._sock is not None:
            return self._sock
        import socket

        host, _, port = self.udp.partition(":")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host or "0.0.0.0", int(port or 5566)))
            s.settimeout(2.0)
            self._sock = s
        except Exception as e:
            raise AdapterError(f"could not bind {self.udp}: {type(e).__name__}: {e}") from e
        return self._sock

    async def _read_lines(self, limit: int = 32) -> list[str]:
        loop = asyncio.get_running_loop()

        def pull() -> list[str]:
            lines: list[str] = []
            if self.port:
                ser = self._open_serial()
                for _ in range(limit):
                    raw = ser.readline()
                    if not raw:
                        break
                    lines.append(raw.decode("utf-8", "replace").strip())
            elif self.udp:
                sock = self._open_udp()
                for _ in range(limit):
                    try:
                        data, _addr = sock.recvfrom(4096)
                    except Exception:
                        break
                    lines.append(data.decode("utf-8", "replace").strip())
            return [l for l in lines if l]

        return await loop.run_in_executor(None, pull)

    # ---------- parsing ----------

    @staticmethod
    def parse_line(line: str) -> dict | None:
        """Pull a subcarrier vector (and RSSI if present) out of one CSV line.

        Located by shape rather than column index, because the layout varies
        between toolkit versions and a fixed index silently misreads.
        """
        if not line or line.startswith(("#", "type", "CSI_DATA,type")):
            return None
        m = VECTOR_RE.search(line)
        if not m:
            return None
        try:
            values = [int(v) for v in m.group(1).replace(" ", "").split(",") if v not in ("", "-")]
        except ValueError:
            return None
        if len(values) < 4:
            return None

        # CSI arrives as interleaved (imaginary, real) pairs; amplitude is the
        # magnitude of each pair.
        amps = [
            (values[i] ** 2 + values[i + 1] ** 2) ** 0.5
            for i in range(0, len(values) - 1, 2)
        ]

        rssi = None
        for field in line.split(",")[:12]:
            f = field.strip()
            if f.lstrip("-").isdigit():
                n = int(f)
                if -100 <= n <= -20:      # plausible dBm
                    rssi = n
                    break

        return {"amplitudes": amps, "rssi": rssi, "subcarriers": len(amps)}

    # ---------- adapter interface ----------

    async def read(self) -> Reading:
        if not (self.port or self.udp):
            raise AdapterError("no ESP32_CSI_PORT or ESP32_CSI_UDP configured")

        lines = await self._read_lines()
        frames = [f for f in (self.parse_line(l) for l in lines) if f]
        if not frames:
            raise AdapterError("no CSI frames received")

        per_frame_mean = [statistics.fmean(f["amplitudes"]) for f in frames if f["amplitudes"]]
        for value in per_frame_mean:
            self._recent.append(value)

        # Variance across the recent window is the motion signal: a still room
        # gives a near-constant channel, a moving body perturbs it.
        variance = statistics.pvariance(self._recent) if len(self._recent) > 1 else 0.0
        rssis = [f["rssi"] for f in frames if f["rssi"] is not None]

        return Reading(
            device_name=self.device_name,
            source=self.source,
            taken_at=Reading.now(),
            watts=None,        # CSI measures motion, never power
            kwh_today=None,
            extra={
                "frames": len(frames),
                "subcarriers": frames[0]["subcarriers"],
                "amplitude_mean": round(statistics.fmean(per_frame_mean), 4) if per_frame_mean else None,
                "amplitude_variance": round(variance, 6),
                "rssi_mean": round(statistics.fmean(rssis), 1) if rssis else None,
                "transport": "serial" if self.port else "udp",
            },
        )

    async def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


def from_env() -> Esp32CsiAdapter | None:
    port = os.environ.get("ESP32_CSI_PORT", "").strip()
    udp = os.environ.get("ESP32_CSI_UDP", "").strip()
    if not (port or udp):
        return None
    return Esp32CsiAdapter(
        device_name=os.environ.get("ESP32_CSI_NAME", "csi").strip() or "csi",
        port=port or None,
        udp=udp or None,
        window=int(os.environ.get("ESP32_CSI_WINDOW", "64")),
    )
