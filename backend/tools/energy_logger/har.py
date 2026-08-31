"""
Human Activity Recognition — turns raw CSI motion + wattage into a label.

The labels are deliberately few and mean something operational, because their
whole purpose is to change what the agents recommend:

    UNOCCUPIED   no motion, load flat at baseline  -> shed freely, nobody is inconvenienced
    IDLE         no motion, load above baseline    -> standby waste; a candidate to cut
    PRESENT      motion, load at baseline          -> someone is here but not using this load
    ACTIVE       motion, load above baseline       -> in use; do not interrupt
    UNKNOWN      not enough evidence               -> never guess

HONESTY RULE
------------
`classify()` returns UNKNOWN unless it actually has the inputs it needs. It
never infers occupancy from wattage alone — a console left on looks identical
to a console being played, and calling that "ACTIVE" would be a fabricated
claim about a person. Confidence is reported alongside every label and is
derived from how far the inputs sit from the thresholds, not invented.

CALIBRATION
-----------
The thresholds below are placeholders and are marked as such at runtime:
`Thresholds.calibrated` is False until they are fitted to a real capture. The
motion threshold in particular is meaningless before a board exists, because
CSI variance scales with the radio, the room and the antenna placement.
`fit_thresholds()` derives them from a labelled capture once you have one.

STATUS: logic complete and unit-testable; thresholds uncalibrated; no CSI
hardware has ever fed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Activity(str, Enum):
    UNOCCUPIED = "unoccupied"
    IDLE = "idle"
    PRESENT = "present"
    ACTIVE = "active"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Thresholds:
    """Decision boundaries. Placeholders until fitted to a real capture."""

    motion_variance: float = 0.05   # CSI amplitude variance above which we call it motion
    baseline_watts: float = 5.0     # standby draw for the monitored load
    active_watts: float = 40.0      # draw that indicates real use
    calibrated: bool = False

    def describe(self) -> str:
        state = "calibrated" if self.calibrated else "UNCALIBRATED placeholder values"
        return (f"motion>{self.motion_variance}, baseline={self.baseline_watts}W, "
                f"active>{self.active_watts}W ({state})")


@dataclass(frozen=True)
class Label:
    activity: Activity
    confidence: float          # 0..1, from distance to the thresholds
    basis: str                 # what drove the decision, for the UI and for debugging
    calibrated: bool

    def as_dict(self) -> dict:
        return {
            "activity": self.activity.value,
            "confidence": round(self.confidence, 3),
            "basis": self.basis,
            "calibrated": self.calibrated,
        }


def classify(
    csi_variance: float | None,
    watts: float | None,
    thresholds: Thresholds | None = None,
) -> Label:
    """Label one moment. Missing inputs produce UNKNOWN, never a guess."""
    t = thresholds or Thresholds()

    if csi_variance is None and watts is None:
        return Label(Activity.UNKNOWN, 0.0, "no CSI and no power reading", t.calibrated)

    if csi_variance is None:
        # Power alone cannot distinguish a person from a device left running.
        return Label(
            Activity.UNKNOWN, 0.0,
            f"power is {watts:.1f}W but there is no motion signal, and power alone "
            f"cannot tell whether anyone is present",
            t.calibrated,
        )

    moving = csi_variance > t.motion_variance
    motion_margin = _margin(csi_variance, t.motion_variance)

    if watts is None:
        activity = Activity.PRESENT if moving else Activity.UNOCCUPIED
        return Label(
            activity, motion_margin * 0.6,
            f"CSI variance {csi_variance:.4f} vs threshold {t.motion_variance}; "
            f"no power reading to say whether anything is in use",
            t.calibrated,
        )

    in_use = watts > t.active_watts
    at_baseline = watts <= t.baseline_watts
    power_margin = _margin(watts, t.active_watts if in_use else max(t.baseline_watts, 1e-6))
    confidence = min(1.0, (motion_margin + power_margin) / 2)

    if moving and in_use:
        activity, why = Activity.ACTIVE, "motion detected and the load is drawing real power"
    elif moving and at_baseline:
        activity, why = Activity.PRESENT, "motion detected but the load is at standby"
    elif moving:
        activity, why = Activity.PRESENT, "motion detected, load between standby and active"
    elif in_use:
        activity, why = Activity.IDLE, "no motion but the load is still drawing power"
    elif at_baseline:
        activity, why = Activity.UNOCCUPIED, "no motion and the load is at standby"
    else:
        activity, why = Activity.IDLE, "no motion, load above standby"

    return Label(
        activity, confidence,
        f"{why} (CSI variance {csi_variance:.4f}, {watts:.1f}W)",
        t.calibrated,
    )


def _margin(value: float, threshold: float) -> float:
    """How decisively a value clears a threshold, squashed to 0..1."""
    if threshold <= 0:
        return 0.5
    ratio = abs(value - threshold) / threshold
    return max(0.0, min(1.0, ratio))


ACTIVE_LABELS = {Activity.ACTIVE.value, Activity.PRESENT.value}
STILL_LABELS = {Activity.UNOCCUPIED.value, Activity.IDLE.value}


def fit_thresholds(samples: list[dict]) -> Thresholds:
    """Derive thresholds from a labelled capture.

    Each sample: {"csi_variance": float, "watts": float, "label": "active"|...}.
    Returns calibrated=True only when both classes are actually represented —
    fitting a boundary from one-sided data would produce a confident wrong answer.
    """
    def pick(field: str, labels: set[str]) -> list[float]:
        return [s[field] for s in samples
                if s.get("label") in labels and s.get(field) is not None]

    moving = pick("csi_variance", ACTIVE_LABELS)
    still = pick("csi_variance", STILL_LABELS)
    on = pick("watts", {Activity.ACTIVE.value})
    off = pick("watts", {Activity.UNOCCUPIED.value})

    if not (moving and still and on and off):
        return Thresholds()   # leave placeholders in place, calibrated=False

    return Thresholds(
        motion_variance=(max(still) + min(moving)) / 2,
        baseline_watts=max(off),
        active_watts=(max(off) + min(on)) / 2,
        calibrated=True,
    )
