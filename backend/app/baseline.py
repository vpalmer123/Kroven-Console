"""
Rolling per-device thresholds — the seed of MOL.

The point: Kroven should not carry a hardcoded idea of what "in use" means.
Each metered device learns its own idle and active levels from its own history,
and the split tightens as readings accumulate. Nothing is trained; this is a
running statistic that is correct on day one with 40 samples and simply sharper
on day thirty with 4,000.

HOW THE SPLIT IS FOUND
----------------------
Idle and active draw are usually two clusters with a gap between them (a console
at 0.4W standby and 84W in use). We find the split with a 1-D two-means pass,
seeded from the low and high percentiles so it converges in a handful of
iterations and cannot wander. If the two clusters are not actually separated —
one mode, or too few points — we say so via `separated=False` and report low
confidence rather than inventing a boundary.

CONFIDENCE
----------
Reported, never assumed. It rises with sample count and with how cleanly the two
clusters separate, and is capped until there are enough samples to mean
anything. A caller that gets confidence 0.2 should treat the threshold as a
hint; the UI shows it rather than hiding it.

ADDING DOMAINS
--------------
`signal_type` is free text, so presence, CSI variance, temperature and whatever
comes next all use this same machinery. Nothing here knows what a watt is.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

MIN_SAMPLES = 12          # below this, a split is noise
GOOD_SAMPLES = 400        # where sample-count confidence saturates


@dataclass
class Baseline:
    source: str
    signal_type: str
    idle_value: float | None
    active_value: float | None
    threshold: float | None
    sample_count: int
    idle_count: int
    active_count: int
    confidence: float
    separated: bool
    note: str

    def as_dict(self) -> dict:
        return asdict(self)

    def classify(self, value: float | None) -> str:
        """idle | active | unknown for one reading."""
        if value is None or self.threshold is None or not self.separated:
            return "unknown"
        return "active" if value >= self.threshold else "idle"


def _two_means(values: list[float], iterations: int = 25) -> tuple[float, float]:
    """1-D k-means with k=2, seeded from percentiles so it is deterministic."""
    ordered = sorted(values)
    lo = ordered[max(0, int(len(ordered) * 0.10))]
    hi = ordered[min(len(ordered) - 1, int(len(ordered) * 0.90))]
    if lo == hi:
        return lo, hi

    for _ in range(iterations):
        low_grp = [v for v in values if abs(v - lo) <= abs(v - hi)]
        high_grp = [v for v in values if abs(v - lo) > abs(v - hi)]
        if not low_grp or not high_grp:
            break
        new_lo = statistics.fmean(low_grp)
        new_hi = statistics.fmean(high_grp)
        if abs(new_lo - lo) < 1e-9 and abs(new_hi - hi) < 1e-9:
            lo, hi = new_lo, new_hi
            break
        lo, hi = new_lo, new_hi
    return lo, hi


def compute(source: str, signal_type: str, values: list[float]) -> Baseline:
    """Derive the current baseline for one device/signal from its readings."""
    clean = [float(v) for v in values if v is not None]
    n = len(clean)

    if n < MIN_SAMPLES:
        return Baseline(source, signal_type, None, None, None, n, 0, 0, 0.0, False,
                        f"only {n} readings; need {MIN_SAMPLES} before a split means anything")

    lo, hi = _two_means(clean)
    if hi <= lo:
        return Baseline(source, signal_type, lo, hi, None, n, 0, 0, 0.0, False,
                        "readings form one cluster; no idle/active split visible yet")

    threshold = (lo + hi) / 2
    idle = [v for v in clean if v < threshold]
    active = [v for v in clean if v >= threshold]

    spread = statistics.pstdev(clean) or 1e-9
    gap = (hi - lo) / spread                       # how many sd apart the modes sit
    separation = max(0.0, min(1.0, gap / 3.0))
    volume = min(1.0, n / GOOD_SAMPLES)
    balance = min(len(idle), len(active)) / max(1, max(len(idle), len(active)))

    confidence = round(min(1.0, separation * 0.5 + volume * 0.3 + balance * 0.2), 3)
    separated = bool(separation > 0.25 and len(idle) >= 3 and len(active) >= 3)

    if separated:
        note = (f"idle around {lo:.4g}, active around {hi:.4g}; "
                f"tightening as readings land")
    else:
        note = "clusters overlap too much to call a reliable threshold yet"

    return Baseline(
        source=source, signal_type=signal_type,
        idle_value=round(lo, 6), active_value=round(hi, 6),
        threshold=round(threshold, 6) if separated else None,
        sample_count=n, idle_count=len(idle), active_count=len(active),
        confidence=confidence if separated else round(confidence * 0.4, 3),
        separated=separated, note=note,
    )


# ---------------------------------------------------------------------------
# Correlation: does a power transition line up with detected presence?
# ---------------------------------------------------------------------------

@dataclass
class Correlation:
    power_events: int
    presence_events: int
    matched: int
    match_rate: float | None
    window_seconds: int
    confidence: float
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


def correlate_presence(power_events: list[datetime],
                       presence_events: list[datetime],
                       window_seconds: int = 300) -> Correlation:
    """How often a power transition coincides with detected presence.

    A spike that lines up with someone being there is higher-confidence real
    activity than one that does not. The match rate is itself a signal worth
    storing, which is why it comes back as a value rather than just a boolean.

    Returns match_rate None when either side has no events — a rate computed
    from nothing would read as 0% and look like a finding.
    """
    if not power_events or not presence_events:
        return Correlation(
            len(power_events), len(presence_events), 0, None, window_seconds, 0.0,
            "need both power transitions and presence events before this means anything",
        )

    presence = sorted(presence_events)
    matched = 0
    for evt in power_events:
        for p in presence:
            if abs((p - evt).total_seconds()) <= window_seconds:
                matched += 1
                break

    rate = matched / len(power_events)
    volume = min(1.0, len(power_events) / 50)
    confidence = round(min(1.0, volume * 0.6 + (0.4 if len(presence) >= 5 else 0.1)), 3)

    if rate >= 0.7:
        note = "power transitions mostly happen while someone is present"
    elif rate <= 0.3:
        note = "power transitions mostly happen with nobody detected — likely scheduled or standby cycling"
    else:
        note = "mixed: some transitions line up with presence, some do not"

    return Correlation(len(power_events), len(presence), matched,
                       round(rate, 3), window_seconds, confidence, note)


def transitions(points: list[tuple[datetime, float]], baseline: Baseline) -> list[datetime]:
    """Times where a device crossed its own threshold, in either direction."""
    if baseline.threshold is None or not baseline.separated:
        return []
    out: list[datetime] = []
    prev_state: str | None = None
    for when, value in sorted(points, key=lambda p: p[0]):
        state = baseline.classify(value)
        if state == "unknown":
            continue
        if prev_state is not None and state != prev_state:
            out.append(when)
        prev_state = state
    return out


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
