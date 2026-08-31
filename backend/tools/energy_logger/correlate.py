"""
Correlation layer — joins activity labels to what the other agents see.

The problem this exists to solve: the Solar, Battery, Grid and Forecast agents
each hold a slice of the picture and none of them knows whether anyone is home.
"Shed the load at 4pm" is a good call in an empty house and a bad one during a
game. This module is the join.

It produces an ActivityContext: a compact, sourced summary that the chat agent
can act on. Two rules it does not break:

  1. It never invents a signal. Every field is either present with a named
     source or absent. Absent is a normal outcome, not an error.
  2. It never upgrades confidence. If the HAR label is UNKNOWN or uncalibrated,
     that propagates all the way to the recommendation, which then declines to
     make an occupancy-dependent call.

STATUS: the join and the recommendation logic work today and are tested against
the live rate engine. The occupancy half is only as good as the CSI hardware,
which does not exist yet — until then `activity` is UNKNOWN and the layer
degrades to rate-only advice, which is exactly what the app already does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .har import Activity, Label, Thresholds, classify


@dataclass
class AgentView:
    """What one agent currently believes, plus where it got it."""

    name: str
    available: bool
    summary: str = ""
    source: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def unavailable(cls, name: str, why: str) -> "AgentView":
        return cls(name=name, available=False, summary=why)


@dataclass
class ActivityContext:
    taken_at: datetime
    label: Label
    watts: float | None
    views: list[AgentView]
    recommendation: str
    actionable: bool

    def as_dict(self) -> dict:
        return {
            "taken_at": self.taken_at.isoformat(),
            "activity": self.label.as_dict(),
            "watts": self.watts,
            "agents": [
                {"name": v.name, "available": v.available, "summary": v.summary,
                 "source": v.source, "data": v.data}
                for v in self.views
            ],
            "recommendation": self.recommendation,
            "actionable": self.actionable,
        }


# --------------------------------------------------------------------------
# Agent views. Each returns available=False rather than a placeholder number.
# --------------------------------------------------------------------------

def grid_view(now: datetime | None = None) -> AgentView:
    """Live tariff position. This one is real today."""
    try:
        from app.rate_data import other_period, pricing_at
    except Exception as e:
        return AgentView.unavailable("grid", f"rate engine unavailable ({type(e).__name__})")

    now = now or datetime.now()
    current = pricing_at(now)
    alt = other_period(now)
    return AgentView(
        name="grid",
        available=True,
        summary=(f"{'higher' if current.period == 'peak' else 'lower'}-priced window now "
                 f"({current.range_text()}); other window {alt.range_text()}"),
        source=f"{current.source}, effective {current.effective}",
        data={
            "period": current.period,
            "price_low": current.low,
            "price_high": current.high,
            "peak_start": current.peak_start.strftime("%H:%M"),
            "peak_end": current.peak_end.strftime("%H:%M"),
        },
    )


def solar_view(profile: dict | None = None) -> AgentView:
    """Solar generation. Unavailable until an inverter integration exists —
    a panel's nameplate size is not a generation reading."""
    assets = (profile or {}).get("assets") or {}
    solar = assets.get("solar") or {}
    if not solar.get("present"):
        return AgentView.unavailable("solar", "no solar reported for this household")
    return AgentView.unavailable(
        "solar",
        f"solar is present ({solar.get('size_kw', '?')} kW) but there is no inverter "
        f"feed, so current generation is unknown",
    )


def battery_view(profile: dict | None = None) -> AgentView:
    """State of charge. Needs a battery API; capacity alone says nothing about SoC."""
    assets = (profile or {}).get("assets") or {}
    batt = assets.get("battery") or {}
    if not batt.get("present"):
        return AgentView.unavailable("battery", "no battery reported for this household")
    return AgentView.unavailable(
        "battery",
        f"battery is present ({batt.get('capacity_kwh', '?')} kWh) but there is no "
        f"telemetry feed, so state of charge is unknown",
    )


def forecast_view(readings: list[dict] | None = None) -> AgentView:
    """Load forecast. Honest about the model actually in use."""
    readings = readings or []
    if len(readings) < 24:
        return AgentView.unavailable(
            "forecast",
            f"only {len(readings)} readings stored; not enough history to forecast from",
        )
    return AgentView(
        name="forecast",
        available=True,
        summary=f"{len(readings)} readings available for forecasting",
        source="energy_readings",
        data={"reading_count": len(readings)},
    )


# --------------------------------------------------------------------------

def build_context(
    csi_variance: float | None,
    watts: float | None,
    profile: dict | None = None,
    readings: list[dict] | None = None,
    thresholds: Thresholds | None = None,
    now: datetime | None = None,
) -> ActivityContext:
    """Join the activity label to every agent view and derive one recommendation."""
    now = now or datetime.now()
    label = classify(csi_variance, watts, thresholds)
    views = [
        grid_view(now),
        solar_view(profile),
        battery_view(profile),
        forecast_view(readings),
    ]
    rec, actionable = _recommend(label, views, watts)
    return ActivityContext(now, label, watts, views, rec, actionable)


def _recommend(label: Label, views: list[AgentView], watts: float | None) -> tuple[str, bool]:
    grid = next((v for v in views if v.name == "grid"), None)

    if grid is None or not grid.available:
        return ("No pricing available, so there is nothing to time a decision against.", False)

    peak = grid.data.get("period") == "peak"

    if label.activity is Activity.UNKNOWN:
        return (
            "No occupancy signal, so no decision that depends on someone being home. "
            + ("Prices are in the higher window now." if peak
               else "Prices are in the lower window now."),
            False,
        )

    if not label.calibrated:
        return (
            f"Activity reads as {label.activity.value}, but the detector is running on "
            f"uncalibrated thresholds — treat it as a hint, not a basis for switching "
            f"anything off.",
            False,
        )

    if label.activity is Activity.ACTIVE:
        return (
            "Someone is using this load right now. Do not shed it, even in the expensive "
            "window — interrupting a person to save cents is the wrong trade.", True)

    if label.activity is Activity.IDLE and peak:
        cost = f" It is drawing {watts:.0f}W." if watts else ""
        return (f"Nobody is here and the load is still on during the expensive window."
                f"{cost} This is the clearest shed opportunity.", True)

    if label.activity is Activity.IDLE:
        return ("Nobody is here but the load is still drawing power. Worth cutting, "
                "though prices are low so the saving is small right now.", True)

    if label.activity is Activity.UNOCCUPIED:
        return ("Nobody home and nothing drawing above standby — nothing to do.", True)

    return ("Someone is here but this load is idle; leave it alone.", True)
