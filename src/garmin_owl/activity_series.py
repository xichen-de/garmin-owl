"""On-demand, location-free per-sample analysis of a single activity.

Everything here is a pure function of the samples parsed from Garmin's activity *details*
(chart) read. The read also carries latitude/longitude columns and a ``geoPolylineDTO`` with
start/end points and a bounding box, even when no polyline is requested. Parsing reads only an
explicit allow-list of descriptor keys, so no location value ever reaches the cache, a model or
a tool response.

The pipeline:

1. ``parse_activity_chart``: map Garmin's columns by descriptor key (never by position) into a
   ``ChartSeries``; a channel the activity did not record is all ``None``.
2. ``analyse``: moving/stopped flags, a smoothed elevation profile over distance, grade over a
   distance window, and per-sample time weights. The cache stores the ``ChartSeries`` and every
   derived value is recomputed on read, so a cache hit answers exactly like a live read.
3. Views: a resampled time series, segments from a ``Segmenter``, whole-activity metrics, and
   matching one window across activities by elevation shape.

Segmentation is behind the small ``Segmenter`` protocol. ``DistanceSegmenter`` splits by the
elevation profile; a time-based work/rest segmenter driven by HR (and respiration) for indoor
sessions can be added beside it without touching the metric code, which works on sample ranges.
"""

from __future__ import annotations

import json
import math
import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from statistics import fmean, median
from typing import Any, Protocol

from .models import (
    ActivitySegment,
    AvailabilityNotice,
    CardiacDrift,
    HrGradeRelation,
    SampleStretch,
    SampleValue,
    SamplingQuality,
)
from .notices import NOT_RECORDED, derived_notice

# --- Location guard ---------------------------------------------------------------------------

# Word tokens that mark a key as location-bearing, in Garmin's camelCase or our snake_case.
LOCATION_TOKENS = frozenset(
    {
        "lat",
        "latitude",
        "lon",
        "lng",
        "long",
        "longitude",
        "position",
        "positions",
        "polyline",
        "geo",
        "point",
        "points",
        "coordinate",
        "coordinates",
        "gps",
        "bbox",
        "bounds",
        "semicircles",
    }
)
_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def is_location_key(key: str) -> bool:
    """Whether a field name refers to a position, a track, or anything derived from them."""
    return any(token.lower() in LOCATION_TOKENS for token in _TOKEN.findall(key))


# --- Garmin column mapping --------------------------------------------------------------------

# Descriptor keys read from the chart response, with alternates Garmin has used. Values arrive
# already in the descriptor's unit (metres, m/s, bpm, seconds); ``unit.factor`` is informational.
_ELAPSED_KEYS = ("sumElapsedDuration",)
_TIMER_KEYS = ("sumDuration",)
_MOVING_KEYS = ("sumMovingDuration",)
_TIMESTAMP_KEYS = ("directTimestamp",)
_CHANNEL_KEYS: dict[str, tuple[str, ...]] = {
    "distance_m": ("sumDistance",),
    "hr_bpm": ("directHeartRate",),
    "speed_mps": ("directSpeed", "directEnhancedSpeed"),
    "elevation_m": ("directElevation", "directEnhancedElevation", "directAltitude"),
    "respiration_brpm": ("directRespirationRate",),
}
# directDoubleCadence is total steps per minute (both feet), which is what Garmin Connect shows
# for walking and running. Cycling cadence is pedal revolutions per minute.
_CADENCE_KEYS = (("directDoubleCadence", "spm"), ("directBikeCadence", "rpm"))
READ_KEYS = frozenset(
    [*_ELAPSED_KEYS, *_TIMER_KEYS, *_MOVING_KEYS, *_TIMESTAMP_KEYS]
    + [key for keys in _CHANNEL_KEYS.values() for key in keys]
    + [key for key, _ in _CADENCE_KEYS]
)
assert not any(is_location_key(key) for key in READ_KEYS)

CHANNELS = ("distance_m", "hr_bpm", "speed_mps", "cadence", "elevation_m", "respiration_brpm")


@dataclass
class ChartSeries:
    """An activity's native samples, sorted by elapsed time. Never holds location."""

    elapsed_s: list[float]
    timer_s: list[float | None]
    moving_s: list[float | None]
    channels: dict[str, list[float | None]]
    cadence_unit: str | None = None
    garmin_total_samples: int | None = None
    start_gmt_ms: float | None = None

    def has(self, channel: str) -> bool:
        return any(value is not None for value in self.channels[channel])

    def sample_count(self) -> int:
        return len(self.elapsed_s)

    @property
    def has_moving_time(self) -> bool:
        return any(value is not None for value in self.moving_s)

    @property
    def has_timer(self) -> bool:
        return any(value is not None for value in self.timer_s)


@dataclass
class ActivitySamples:
    """What the cache stores for one activity: its identity and location-free samples."""

    activity_id: int
    start_time: str | None
    activity_type: str | None
    series: ChartSeries


def _number(row: Any, position: int) -> float | None:
    metrics = row.get("metrics") if isinstance(row, dict) else None
    if not isinstance(metrics, list) or position >= len(metrics):
        return None
    value = metrics[position]
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _nothing_measured(values: list[float | None]) -> list[float | None]:
    """Garmin sends zeros for metrics an activity never measured (indoor distance, say)."""
    present = [value for value in values if value is not None]
    return [None] * len(values) if not present or max(present) <= 0 else values


def parse_activity_chart(raw: Any) -> ChartSeries | None:
    """Read the allow-listed, location-free columns of a chart response; None if it has none."""
    if not isinstance(raw, dict):
        return None
    descriptors, rows = raw.get("metricDescriptors"), raw.get("activityDetailMetrics")
    if not isinstance(descriptors, list) or not isinstance(rows, list) or not rows:
        return None
    index: dict[str, int] = {}
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            continue
        key, position = descriptor.get("key"), descriptor.get("metricsIndex")
        if (
            isinstance(key, str)
            and key in READ_KEYS
            and isinstance(position, int)
            and not isinstance(position, bool)
            and position >= 0
        ):
            index[key] = position

    def column(keys: Iterable[str]) -> list[float | None] | None:
        for key in keys:
            if key in index:
                return [_number(row, index[key]) for row in rows]
        return None

    none: list[float | None] = [None] * len(rows)
    elapsed = column(_ELAPSED_KEYS)
    stamps = column(_TIMESTAMP_KEYS)
    if elapsed is None and stamps is not None:
        first = next((stamp for stamp in stamps if stamp is not None), None)
        if first is not None:
            elapsed = [None if s is None else (s - first) / 1000 for s in stamps]
    if elapsed is None:
        return None
    start_gmt_ms = None
    if stamps is not None:
        start_gmt_ms = next(
            (s - e * 1000 for s, e in zip(stamps, elapsed, strict=True) if s and e is not None),
            None,
        )
    columns: dict[str, list[float | None]] = {
        channel: column(keys) or none for channel, keys in _CHANNEL_KEYS.items()
    }
    cadence_unit = None
    columns["cadence"] = none
    for key, unit in _CADENCE_KEYS:
        if key in index:
            columns["cadence"], cadence_unit = column([key]) or none, unit
            break
    timer, moving = column(_TIMER_KEYS) or none, column(_MOVING_KEYS) or none

    order = sorted(
        (i for i, value in enumerate(elapsed) if value is not None and value >= 0),
        key=lambda i: elapsed[i] or 0.0,
    )
    if not order:
        return None

    def pick(values: list[float | None]) -> list[float | None]:
        return [values[i] for i in order]

    channels = {name: pick(values) for name, values in columns.items()}
    for name in ("hr_bpm", "respiration_brpm"):
        # A zero heart or breathing rate is a sensor dropout, not a reading.
        channels[name] = [v if v is not None and v > 0 else None for v in channels[name]]
    for name in ("distance_m", "speed_mps", "cadence"):
        channels[name] = _nothing_measured(channels[name])
    if all(v in (None, 0.0) for v in channels["elevation_m"]):
        channels["elevation_m"] = [None] * len(order)
    if not any(v is not None for v in channels["cadence"]):
        cadence_unit = None
    total = raw.get("totalMetricsCount")
    return ChartSeries(
        elapsed_s=[float(elapsed[i] or 0.0) for i in order],
        timer_s=pick(timer),
        moving_s=_nothing_measured(pick(moving)),
        channels=channels,
        cadence_unit=cadence_unit,
        garmin_total_samples=total
        if isinstance(total, int) and not isinstance(total, bool)
        else None,
        start_gmt_ms=start_gmt_ms,
    )


# --- Signal processing parameters -------------------------------------------------------------

# Below a slow shuffle (about 1 km/h). GPS jitter while standing still stays under it.
MOVING_SPEED_MPS = 0.3
# A sample interval counts as moving when Garmin's moving time advanced for at least half of it.
MOVING_FRACTION = 0.5
# Median of 5 consecutive samples removes single-sample barometric spikes before averaging.
ELEVATION_MEDIAN_SAMPLES = 5
# Moving-average window over distance. Barometric noise is sub-metre per sample; 25 m averages
# it out at walking and running sample spacing while keeping a short steep ramp visible.
ELEVATION_SMOOTHING_M = 25.0
# Grade is the smoothed elevation change across this distance, never sample to sample.
GRADE_WINDOW_M = 30.0
# Near the start or end the window is cut short; below this length grade is not reported.
MIN_GRADE_WINDOW_M = 10.0
# Steeper than this is implausible on foot or bike (the steepest streets are about 35-40 %),
# so such grades come from elevation glitches and are clamped and counted.
MAX_PLAUSIBLE_GRADE_PCT = 45.0
# Interpolating HR or grade across a longer gap would invent data.
MAX_INTERP_GAP_S = 15.0
# Each sample represents half the interval to each neighbour, capped so a pause or recording
# gap does not give one sample minutes of weight.
MAX_SAMPLE_HALF_WEIGHT_S = MAX_INTERP_GAP_S / 2
# A median interval up to this means 1-second recording; above it, smart recording.
EVERY_SECOND_MAX_MEDIAN_S = 1.5
# An interval this many times the median, and at least RECORDING_GAP_MIN_S, is a recording gap.
RECORDING_GAP_FACTOR = 4.0
RECORDING_GAP_MIN_S = 20.0
# Shorter stops (a brief pause at a crossing) are not listed individually.
MIN_STRETCH_S = 10.0
MAX_LISTED_STRETCHES = 20

# Segmentation. An up or down leg must change elevation by at least min_climb_m (a tool
# parameter, default 5 m) and average at least this grade to be a climb or descent.
MIN_CLIMB_GRADE_PCT = 1.5
# Within a leg, leading/trailing samples within this height of its start/end are flat run-in.
FLAT_TOLERANCE_M = 1.0
# HR keeps rising for tens of seconds after a climb ends; search this long past the end.
HR_PEAK_SEARCH_AFTER_S = 90.0
# Recovery is the HR drop over this time after a climb ends.
HR_RECOVERY_S = 60.0

# Cardiac drift: HR is still settling during the first minutes, so they are excluded; only
# flat, steadily paced moving samples count, and there must be enough of them.
DECOUPLING_WARMUP_S = 300.0
DECOUPLING_FLAT_GRADE_PCT = 2.0
DECOUPLING_SPEED_TOLERANCE = 0.15
DECOUPLING_MIN_SECONDS = 600.0
# HR reflects the last minute or two of effort, so a flat sample right after a climb still
# carries the climb's HR. Only samples flat for this long before them count.
DECOUPLING_FLAT_HISTORY_S = 120.0

# HR vs grade: both are resampled to a regular grid (smart recording samples more densely when
# things change, which would bias a per-sample fit). HR is paired with the grade some seconds
# earlier; the lag with the strongest correlation in this range is used and reported.
HR_GRADE_GRID_S = 5
HR_GRADE_LAGS_S = tuple(range(0, 91, 5))
HR_GRADE_MIN_POINTS = 60
HR_GRADE_MIN_SPREAD_PCT = 1.0

# Segment matching without GPS: the reference window's smoothed elevation sampled every
# PROFILE_STEP_M is compared against windows shifted by up to MATCH_SEARCH_M (or 10 % of the
# start distance, if larger) and stretched by a few percent, since GPS distance to the same
# point differs from day to day.
PROFILE_STEP_M = 10.0
MATCH_SEARCH_M = 200.0
MATCH_SEARCH_FRACTION = 0.1
MATCH_LENGTH_SCALES = (0.94, 0.96, 0.98, 1.0, 1.02, 1.04, 1.06)
MIN_MATCH_CONFIDENCE = 0.6
# A window with less relief than this has no shape to match; it is matched by distance alone.
MIN_PROFILE_RELIEF_M = 3.0
MIN_MATCH_WINDOW_M = 50.0

# Time series output.
MAX_TIMESERIES_ROWS = 600
# Rows are the bulk of a time-series response; the rest stays well under the 40k budget.
TIMESERIES_ROWS_CHAR_BUDGET = 32_000
NICE_INTERVALS_S = (1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300, 600, 900, 1800, 3600)
MAX_LISTED_SEGMENTS = 60


# --- Helpers ----------------------------------------------------------------------------------


def _round(value: float | None, digits: int = 1) -> float | None:
    if value is None:
        return None
    rounded = round(value, digits)
    return rounded + 0.0  # never -0.0


def _interp(xs: Sequence[float], ys: Sequence[float], x: float) -> float | None:
    if not xs or x < xs[0] or x > xs[-1]:
        return None
    j = bisect_left(xs, x)
    if xs[j] == x:
        return ys[j]
    x0, x1 = xs[j - 1], xs[j]
    return ys[j - 1] + (ys[j] - ys[j - 1]) * (x - x0) / (x1 - x0)


class Signal:
    """One channel over elapsed time, interpolated only across short gaps."""

    def __init__(self, xs: Sequence[float], ys: Sequence[float | None]) -> None:
        pairs = [(x, y) for x, y in zip(xs, ys, strict=True) if y is not None]
        self.x = [x for x, _ in pairs]
        self.y = [y for _, y in pairs]

    def at(self, x: float) -> float | None:
        if not self.x or x < self.x[0] or x > self.x[-1]:
            return None
        j = bisect_left(self.x, x)
        if self.x[j] == x:
            return self.y[j]
        if self.x[j] - self.x[j - 1] > MAX_INTERP_GAP_S:
            return None
        return _interp(self.x, self.y, x)


def _rolling_median(values: Sequence[float | None], window: int) -> list[float | None]:
    half = window // 2
    result: list[float | None] = []
    for i, value in enumerate(values):
        near = [v for v in values[max(0, i - half) : i + half + 1] if v is not None]
        result.append(median(near) if value is not None and near else None)
    return result


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 3:
        return None
    mx, my = fmean(xs), fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sxx * syy)


class ElevationProfile:
    """Smoothed elevation as a function of distance: rolling median, then a distance window."""

    def __init__(self, distance: Sequence[float | None], elevation: Sequence[float | None]):
        filtered = _rolling_median(elevation, ELEVATION_MEDIAN_SAMPLES)
        grouped: dict[float, list[float]] = {}
        for d, e in zip(distance, filtered, strict=True):
            if d is not None and e is not None:
                # Standing still repeats one distance; average it into a single point.
                grouped.setdefault(d, []).append(e)
        self.d = sorted(grouped)
        raw = [fmean(grouped[d]) for d in self.d]
        prefix = [0.0]
        for value in raw:
            prefix.append(prefix[-1] + value)
        half = ELEVATION_SMOOTHING_M / 2
        self.e: list[float] = []
        for d in self.d:
            lo, hi = bisect_left(self.d, d - half), bisect_right(self.d, d + half)
            self.e.append((prefix[hi] - prefix[lo]) / (hi - lo))

    @property
    def usable(self) -> bool:
        return len(self.d) >= 2 and self.d[-1] - self.d[0] >= MIN_GRADE_WINDOW_M

    def at(self, distance: float) -> float | None:
        return _interp(self.d, self.e, distance)

    def grade(self, distance: float) -> float | None:
        lo = max(distance - GRADE_WINDOW_M / 2, self.d[0])
        hi = min(distance + GRADE_WINDOW_M / 2, self.d[-1])
        if hi - lo < MIN_GRADE_WINDOW_M:
            return None
        top, bottom = self.at(hi), self.at(lo)
        if top is None or bottom is None:
            return None
        return (top - bottom) / (hi - lo) * 100


# --- Analysis ---------------------------------------------------------------------------------


@dataclass
class Analysis:
    series: ChartSeries
    moving: list[bool] | None
    elevation: list[float | None] | None
    grade: list[float | None] | None
    grade_clamped: int
    profile: ElevationProfile | None
    weights: list[float]
    hr: Signal

    @property
    def elapsed(self) -> list[float]:
        return self.series.elapsed_s

    def channel(self, name: str) -> list[float | None]:
        return self.series.channels[name]

    @property
    def has_distance(self) -> bool:
        return self.series.has("distance_m")


def _moving_flags(series: ChartSeries) -> list[bool] | None:
    elapsed, n = series.elapsed_s, len(series.elapsed_s)
    speed = series.channels["speed_mps"]
    distance = series.channels["distance_m"]
    if not (series.has_moving_time or series.has("speed_mps") or series.has("distance_m")):
        return None
    flags = [False] * n
    for i in range(1, n):
        dt = elapsed[i] - elapsed[i - 1]
        m0, m1 = series.moving_s[i - 1], series.moving_s[i]
        if dt <= 0:
            flags[i] = flags[i - 1]
        elif m0 is not None and m1 is not None:
            flags[i] = m1 - m0 >= MOVING_FRACTION * dt
        elif speed[i] is not None:
            flags[i] = (speed[i] or 0.0) >= MOVING_SPEED_MPS
        elif distance[i] is not None and distance[i - 1] is not None:
            flags[i] = ((distance[i] or 0.0) - (distance[i - 1] or 0.0)) / dt >= MOVING_SPEED_MPS
        else:
            flags[i] = flags[i - 1]
    if n > 1:
        flags[0] = flags[1]
    return flags


def _weights(elapsed: Sequence[float]) -> list[float]:
    n = len(elapsed)
    weights = []
    for i in range(n):
        before = min(elapsed[i] - elapsed[i - 1], 2 * MAX_SAMPLE_HALF_WEIGHT_S) if i else 0.0
        after = min(elapsed[i + 1] - elapsed[i], 2 * MAX_SAMPLE_HALF_WEIGHT_S) if i < n - 1 else 0
        weights.append((before + after) / 2 if n > 1 else 1.0)
    return weights


def analyse(series: ChartSeries) -> Analysis:
    moving = _moving_flags(series)
    distance = series.channels["distance_m"]
    elevation = series.channels["elevation_m"]
    profile: ElevationProfile | None = None
    smoothed: list[float | None] | None = None
    grade: list[float | None] | None = None
    clamped = 0
    if series.has("elevation_m"):
        if series.has("distance_m"):
            profile = ElevationProfile(distance, elevation)
            if profile.usable:
                smoothed = [None if d is None else profile.at(d) for d in distance]
                grade = []
                for i, d in enumerate(distance):
                    value = (
                        profile.grade(d)
                        if d is not None and (moving is None or moving[i])
                        else None
                    )
                    if value is not None and abs(value) > MAX_PLAUSIBLE_GRADE_PCT:
                        clamped += 1
                        value = math.copysign(MAX_PLAUSIBLE_GRADE_PCT, value)
                    grade.append(value)
            else:
                profile = None
        if smoothed is None:
            # No distance axis: smooth over samples only, and report no grade.
            smoothed = _rolling_median(elevation, ELEVATION_MEDIAN_SAMPLES)
    return Analysis(
        series=series,
        moving=moving,
        elevation=smoothed,
        grade=grade,
        grade_clamped=clamped,
        profile=profile,
        weights=_weights(series.elapsed_s),
        hr=Signal(series.elapsed_s, series.channels["hr_bpm"]),
    )


def _weighted_mean(
    values: Sequence[float | None], weights: Sequence[float], indices: Iterable[int]
) -> float | None:
    total = weight = 0.0
    for i in indices:
        value = values[i]
        if value is not None and weights[i] > 0:
            total += value * weights[i]
            weight += weights[i]
    return total / weight if weight else None


# --- Sampling quality -------------------------------------------------------------------------


def _public_name(channel: str, cadence_unit: str | None) -> str:
    return f"cadence_{cadence_unit or 'spm'}" if channel == "cadence" else channel


def _stretches(a: Analysis) -> list[SampleStretch]:
    elapsed, series = a.elapsed, a.series
    intervals = [b - c for b, c in zip(elapsed[1:], elapsed, strict=False) if b > c]
    typical = median(intervals) if intervals else 1.0
    gap = max(RECORDING_GAP_MIN_S, RECORDING_GAP_FACTOR * typical)
    distance = a.channel("distance_m")
    stretches: list[SampleStretch] = []
    current: tuple[str, int, int] | None = None

    def close(item: tuple[str, int, int] | None) -> None:
        if item is None:
            return
        kind, start, end = item
        duration = elapsed[end] - elapsed[start]
        if duration >= MIN_STRETCH_S:
            stretches.append(
                SampleStretch(
                    kind=kind,
                    start_elapsed_s=_round(elapsed[start], 0) or 0.0,
                    duration_s=_round(duration, 0) or 0.0,
                    start_distance_m=_round(distance[start]),
                )
            )

    for i in range(1, len(elapsed)):
        dt = elapsed[i] - elapsed[i - 1]
        t0, t1 = series.timer_s[i - 1], series.timer_s[i]
        kind: str | None = None
        if dt > 0 and t0 is not None and t1 is not None and t1 - t0 < 0.5 * dt:
            kind = "timer_paused"
        elif dt >= gap:
            kind = "recording_gap"
        elif a.moving is not None and not a.moving[i]:
            kind = "stopped"
        if current is not None and kind == current[0]:
            current = (kind, current[1], i)
            continue
        close(current)
        current = (kind, i - 1, i) if kind is not None else None
    close(current)
    return stretches


def sampling_quality(a: Analysis) -> SamplingQuality:
    elapsed = a.elapsed
    intervals = [b - c for b, c in zip(elapsed[1:], elapsed, strict=False) if b > c]
    typical = median(intervals) if intervals else None
    unit = a.series.cadence_unit
    recorded = [_public_name(c, unit) for c in CHANNELS if a.series.has(c)]
    missing = [_public_name(c, unit) for c in CHANNELS if not a.series.has(c)]
    stretches = _stretches(a)
    total = a.series.garmin_total_samples
    return SamplingQuality(
        native_samples=len(elapsed),
        garmin_total_samples=total if total is not None and total != len(elapsed) else None,
        median_interval_s=_round(typical),
        max_interval_s=_round(max(intervals)) if intervals else None,
        recording=(
            "every_second"
            if typical is not None and typical <= EVERY_SECOND_MAX_MEDIAN_S
            else "smart_recording"
        ),
        recorded_metrics=recorded,
        not_recorded_metrics=missing,
        stretches=stretches[:MAX_LISTED_STRETCHES],
        stretches_total=len(stretches) if len(stretches) > MAX_LISTED_STRETCHES else None,
        grade_clamped_samples=a.grade_clamped or None,
    )


_NOT_RECORDED_REASON = {
    "distance_m": "no distance (typical for indoor activities); distance, speed-based pace "
    "and grade are omitted",
    "elevation_m": "no elevation; elevation and grade are omitted",
    "speed_mps": "no speed; speed and pace are omitted",
    "hr_bpm": "no heart rate",
    "respiration_brpm": "no respiration rate (Garmin records it only for some activities and "
    "sensors)",
}


def availability_notices(a: Analysis) -> list[AvailabilityNotice]:
    """Why metrics are absent, and how garmin-owl derived the ones Garmin does not report."""
    notices: list[AvailabilityNotice] = []
    unit = a.series.cadence_unit
    for channel in CHANNELS:
        if not a.series.has(channel):
            name = _public_name(channel, unit)
            reason = _NOT_RECORDED_REASON.get(channel, "no cadence")
            notices.append(
                AvailabilityNotice(
                    field=name,
                    status=NOT_RECORDED,
                    message=f"Garmin's per-sample data for this activity has {reason}. "
                    "Omitted rather than reported as zero.",
                )
            )
    total = a.series.garmin_total_samples
    if total is not None and total > len(a.elapsed):
        notices.append(
            AvailabilityNotice(
                field="sampling",
                status="garmin_downsampled",
                message=f"Garmin returned {len(a.elapsed)} of the {total} samples it stores.",
            )
        )
    if a.series.has("elevation_m") and a.elevation is not None and a.profile is None:
        notices.append(
            AvailabilityNotice(
                field="grade_pct",
                status=MISSING_GRADE,
                message="Elevation was recorded without a usable distance axis, so grade "
                "cannot be computed.",
            )
        )
    return notices


def derived_notices(columns: Iterable[str]) -> list[AvailabilityNotice]:
    formulas = {
        "moving": "true when Garmin's moving time advanced for at least half of the interval "
        f"(or, without it, speed >= {MOVING_SPEED_MPS} m/s).",
        "elevation_m": f"Garmin's elevation smoothed by a {ELEVATION_MEDIAN_SAMPLES}-sample "
        f"rolling median and a {ELEVATION_SMOOTHING_M:g} m distance moving average.",
        "grade_pct": f"smoothed elevation change across {GRADE_WINDOW_M:g} m of distance, moving "
        f"samples only, clamped to ±{MAX_PLAUSIBLE_GRADE_PCT:g} %.",
        "pace_s_per_km": "1000 / speed_mps, only for moving rows.",
    }
    return [derived_notice(c, formulas[c]) for c in columns if c in formulas]


MISSING_GRADE = "not_derivable"


# --- Time series ------------------------------------------------------------------------------

TIMESERIES_COLUMNS = (
    "elapsed_s",
    "moving",
    "distance_m",
    "hr_bpm",
    "speed_mps",
    "pace_s_per_km",
    "cadence_spm",
    "cadence_rpm",
    "elevation_m",
    "grade_pct",
    "respiration_brpm",
)


def available_columns(a: Analysis) -> list[str]:
    series, present = a.series, ["elapsed_s"]
    if a.moving is not None:
        present.append("moving")
    for channel in ("distance_m", "hr_bpm", "speed_mps"):
        if series.has(channel):
            present.append(channel)
    if series.has("speed_mps"):
        present.append("pace_s_per_km")
    if series.has("cadence"):
        present.append(_public_name("cadence", series.cadence_unit))
    if a.elevation is not None:
        present.append("elevation_m")
    if a.grade is not None:
        present.append("grade_pct")
    if series.has("respiration_brpm"):
        present.append("respiration_brpm")
    return present


def _mean(values: Sequence[float | None], members: Sequence[int]) -> float | None:
    present = [values[i] for i in members if values[i] is not None]
    return fmean(v for v in present if v is not None) if present else None


def _cell(a: Analysis, column: str, members: Sequence[int]) -> SampleValue:
    if column == "elapsed_s":
        return round(fmean(a.elapsed[i] for i in members))
    if column == "moving":
        assert a.moving is not None
        return sum(a.moving[i] for i in members) * 2 >= len(members)
    if column == "pace_s_per_km":
        speed = _mean(a.channel("speed_mps"), members)
        moving = a.moving is None or sum(a.moving[i] for i in members) * 2 >= len(members)
        return round(1000 / speed) if moving and speed and speed >= MOVING_SPEED_MPS else None
    if column in ("cadence_spm", "cadence_rpm"):
        value = _mean(a.channel("cadence"), members)
        return None if value is None else round(value)
    if column == "hr_bpm":
        value = _mean(a.channel("hr_bpm"), members)
        return None if value is None else round(value)
    if column == "elevation_m":
        assert a.elevation is not None
        return _round(_mean(a.elevation, members))
    if column == "grade_pct":
        assert a.grade is not None
        return _round(_mean(a.grade, members))
    if column == "speed_mps":
        return _round(_mean(a.channel("speed_mps"), members), 2)
    return _round(_mean(a.channel(column), members))


def _bin_rows(
    a: Analysis, indices: Sequence[int], interval: int, columns: Sequence[str]
) -> list[list[SampleValue]]:
    groups: dict[int, list[int]] = {}
    for i in indices:
        groups.setdefault(int(a.elapsed[i] // interval), []).append(i)
    return [[_cell(a, column, members) for column in columns] for members in groups.values()]


@dataclass
class ResampledSeries:
    interval: int
    columns: list[str]
    rows: list[list[SampleValue]]
    notices: list[AvailabilityNotice]


def resample(
    a: Analysis, indices: Sequence[int], requested: int, columns: Sequence[str]
) -> ResampledSeries:
    """Average samples into fixed intervals, coarsening until under the row and size caps."""
    notices: list[AvailabilityNotice] = []
    interval = requested
    span = a.elapsed[indices[-1]] - a.elapsed[indices[0]] if indices else 0.0
    if span / interval + 1 > MAX_TIMESERIES_ROWS:
        interval = next(
            (n for n in NICE_INTERVALS_S if n >= interval and span / n + 1 <= MAX_TIMESERIES_ROWS),
            NICE_INTERVALS_S[-1],
        )
    while True:
        rows = _bin_rows(a, indices, interval, columns)
        size = len(json.dumps(rows, separators=(",", ":")))
        larger = [n for n in NICE_INTERVALS_S if n > interval]
        if (len(rows) <= MAX_TIMESERIES_ROWS and size <= TIMESERIES_ROWS_CHAR_BUDGET) or not larger:
            break
        interval = larger[0]
    if interval != requested:
        notices.append(
            AvailabilityNotice(
                field="resample_seconds",
                status="coarsened",
                message=f"Resampled every {interval} s instead of the requested {requested} s to "
                f"stay within {MAX_TIMESERIES_ROWS} rows and the response size budget. Narrow "
                "the distance window or request fewer metrics for finer detail.",
            )
        )
    intervals = [b - c for b, c in zip(a.elapsed[1:], a.elapsed, strict=False) if b > c]
    typical = median(intervals) if intervals else None
    if typical is not None and interval < typical:
        notices.append(
            AvailabilityNotice(
                field="resample_seconds",
                status="finer_than_native",
                message=f"Samples are a median {typical:.1f} s apart, so some {interval} s "
                "intervals contain no sample. Those intervals are absent, not interpolated.",
            )
        )
    return ResampledSeries(interval, list(columns), rows, notices)


def window_indices(a: Analysis, start_m: float | None, end_m: float | None) -> list[int]:
    if start_m is None and end_m is None:
        return list(range(len(a.elapsed)))
    lo = start_m if start_m is not None else -math.inf
    hi = end_m if end_m is not None else math.inf
    distance = a.channel("distance_m")
    return [i for i, d in enumerate(distance) if d is not None and lo <= d <= hi]


# --- Segmentation -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """A run of samples, inclusive at both ends. Adjacent spans share their boundary sample."""

    kind: str
    start: int
    end: int


class Segmenter(Protocol):
    name: str

    def unavailable_reason(self, a: Analysis) -> str | None: ...

    def split(self, a: Analysis) -> list[Span]: ...


def _pivots(e: Sequence[float], threshold: float) -> list[int]:
    """Turning points of a zig-zag that ignores reversals smaller than ``threshold``."""
    n = len(e)
    pivots, trend, lo, hi, extreme = [0], 0, 0, 0, 0
    for i in range(1, n):
        if trend == 0:
            hi = i if e[i] > e[hi] else hi
            lo = i if e[i] < e[lo] else lo
            if e[hi] - e[lo] >= threshold:
                trend, first, extreme = (1, lo, hi) if hi > lo else (-1, hi, lo)
                if first != 0:
                    pivots.append(first)
        elif trend == 1:
            if e[i] >= e[extreme]:
                extreme = i
            elif e[extreme] - e[i] >= threshold:
                pivots.append(extreme)
                trend, extreme = -1, i
        else:
            if e[i] <= e[extreme]:
                extreme = i
            elif e[i] - e[extreme] >= threshold:
                pivots.append(extreme)
                trend, extreme = 1, i
    if trend and extreme != pivots[-1]:
        pivots.append(extreme)
    if pivots[-1] != n - 1:
        pivots.append(n - 1)
    return pivots


class DistanceSegmenter:
    """Climb / flat / descent from the smoothed elevation profile over distance."""

    name = "distance_elevation"

    def __init__(self, min_climb_m: float) -> None:
        self.min_climb_m = min_climb_m

    def unavailable_reason(self, a: Analysis) -> str | None:
        if not a.has_distance:
            return "no distance was recorded (typical for indoor activities)"
        if not a.series.has("elevation_m"):
            return "no elevation was recorded"
        if a.profile is None:
            return "the activity covers too little distance for an elevation profile"
        return None

    def split(self, a: Analysis) -> list[Span]:
        assert a.elevation is not None
        distance = a.channel("distance_m")
        located = [
            i
            for i in range(len(a.elapsed))
            if distance[i] is not None and a.elevation[i] is not None
        ]
        # Standing still repeats one distance. Keep the last sample of each run so a segment
        # starts when movement does; stops inside a segment stay within its sample range.
        valid = [
            i
            for k, i in enumerate(located)
            if k == len(located) - 1 or distance[located[k + 1]] != distance[i]
        ]
        if len(valid) < 2:
            return []
        e = [a.elevation[i] or 0.0 for i in valid]
        d = [distance[i] or 0.0 for i in valid]
        pieces: list[tuple[str, int, int]] = []
        pivots = _pivots(e, self.min_climb_m)
        for p0, p1 in pairwise(pivots):
            delta = e[p1] - e[p0]
            if abs(delta) < self.min_climb_m:
                pieces.append(("flat", p0, p1))
                continue
            s, t = p0, p1
            while s < p1 and abs(e[s + 1] - e[p0]) <= FLAT_TOLERANCE_M:
                s += 1
            while t > s and abs(e[t - 1] - e[p1]) <= FLAT_TOLERANCE_M:
                t -= 1
            length = d[t] - d[s]
            grade = (e[t] - e[s]) / length * 100 if length > 0 else 0.0
            if abs(grade) < MIN_CLIMB_GRADE_PCT:
                pieces.append(("flat", p0, p1))
                continue
            if s > p0:
                pieces.append(("flat", p0, s))
            pieces.append(("climb" if delta > 0 else "descent", s, t))
            if t < p1:
                pieces.append(("flat", t, p1))
        merged: list[tuple[str, int, int]] = []
        for kind, s, t in pieces:
            if t <= s:
                continue
            if merged and merged[-1][0] == kind == "flat":
                merged[-1] = ("flat", merged[-1][1], t)
            else:
                merged.append((kind, s, t))
        return [Span(kind, valid[s], valid[t]) for kind, s, t in merged]


def _kind_from_grade(grade: float | None) -> str:
    if grade is None or abs(grade) < MIN_CLIMB_GRADE_PCT:
        return "flat"
    return "climb" if grade > 0 else "descent"


def segment_metrics(a: Analysis, span: Span) -> ActivitySegment:
    elapsed, i0, i1 = a.elapsed, span.start, span.end
    t0, t1 = elapsed[i0], elapsed[i1]
    members = range(i0, i1 + 1)
    distance = a.channel("distance_m")
    d0, d1 = distance[i0], distance[i1]
    length = d1 - d0 if d0 is not None and d1 is not None else None

    gain = loss = None
    grade = None
    if a.elevation is not None and a.profile is not None:
        heights = [a.elevation[i] for i in members if a.elevation[i] is not None]
        steps = [
            b - c
            for b, c in zip(heights[1:], heights, strict=False)
            if b is not None and c is not None
        ]
        gain = sum(s for s in steps if s > 0)
        loss = -sum(s for s in steps if s < 0)
        e0, e1 = a.elevation[i0], a.elevation[i1]
        if length and e0 is not None and e1 is not None:
            grade = (e1 - e0) / length * 100

    moving_seconds = None
    if a.moving is not None:
        moving_seconds = sum(
            elapsed[i] - elapsed[i - 1] for i in range(i0 + 1, i1 + 1) if a.moving[i]
        )
    moving_members = [i for i in members if a.moving is None or a.moving[i]]

    hr = a.channel("hr_bpm")
    search_end = t1 + (HR_PEAK_SEARCH_AFTER_S if span.kind == "climb" else 0.0)
    peak_i = None
    for i in range(i0, len(elapsed)):
        if elapsed[i] > search_end:
            break
        value = hr[i]
        if value is not None and (peak_i is None or value > (hr[peak_i] or 0.0)):
            peak_i = i
    hr_drop = None
    if span.kind == "climb":
        at_end, later = a.hr.at(t1), a.hr.at(t1 + HR_RECOVERY_S)
        if at_end is not None and later is not None:
            hr_drop = at_end - later
    speed = length / moving_seconds if length and moving_seconds else None
    cadence = _weighted_mean(a.channel("cadence"), a.weights, moving_members)
    respiration = a.channel("respiration_brpm")
    breaths = [respiration[i] for i in members if respiration[i] is not None]
    unit = a.series.cadence_unit
    return ActivitySegment(
        kind=span.kind,
        start_distance_m=_round(d0),
        end_distance_m=_round(d1),
        length_m=_round(length),
        start_elapsed_s=_round(t0, 0) or 0.0,
        end_elapsed_s=_round(t1, 0) or 0.0,
        duration_s=_round(t1 - t0, 0) or 0.0,
        moving_seconds=_round(moving_seconds, 0),
        elevation_gain_m=_round(gain),
        elevation_loss_m=_round(loss),
        average_grade_pct=_round(grade),
        average_hr_bpm=_round(_weighted_mean(hr, a.weights, members), 0),
        hr_at_start_bpm=_round(a.hr.at(t0), 0),
        peak_hr_bpm=_round(hr[peak_i], 0) if peak_i is not None else None,
        hr_peak_lag_s=_round(elapsed[peak_i] - t0, 0) if peak_i is not None else None,
        hr_peak_after_end_s=(
            _round(elapsed[peak_i] - t1, 0) if peak_i is not None and elapsed[peak_i] > t1 else None
        ),
        hr_drop_60s_after_end_bpm=_round(hr_drop, 0),
        average_speed_mps=_round(speed, 2),
        pace_s_per_km=_round(1000 / speed, 0) if speed and speed >= MOVING_SPEED_MPS else None,
        average_cadence_spm=_round(cadence, 0) if unit == "spm" else None,
        average_cadence_rpm=_round(cadence, 0) if unit == "rpm" else None,
        average_respiration_brpm=_round(_weighted_mean(respiration, a.weights, members)),
        peak_respiration_brpm=_round(max(b for b in breaths if b is not None)) if breaths else None,
    )


# --- Whole-activity metrics -------------------------------------------------------------------


def cardiac_drift(a: Analysis) -> CardiacDrift:
    def skipped(reason: str, seconds: float | None = None) -> CardiacDrift:
        return CardiacDrift(status="skipped", reason=reason, qualifying_seconds=_round(seconds, 0))

    if not a.series.has("hr_bpm"):
        return skipped("no heart rate was recorded")
    if not a.series.has("speed_mps") or a.moving is None:
        return skipped("no speed was recorded")
    if a.grade is None:
        return skipped("flat sections cannot be identified without distance and elevation")
    speed, hr = a.channel("speed_mps"), a.channel("hr_bpm")
    moving_speeds = [s for i, s in enumerate(speed) if s is not None and a.moving[i]]
    if not moving_speeds:
        return skipped("the activity has no moving samples")
    typical = median(moving_speeds)
    grade = a.grade
    elapsed = a.elapsed
    # Latest time of a moving sample steeper than flat, at or before each sample.
    last_steep: list[float] = []
    latest = -math.inf
    for i in range(len(elapsed)):
        g = grade[i]
        if g is not None and abs(g) > DECOUPLING_FLAT_GRADE_PCT:
            latest = elapsed[i]
        last_steep.append(latest)
    qualifying = [
        i
        for i in range(len(a.elapsed))
        if a.moving[i]
        and a.elapsed[i] >= DECOUPLING_WARMUP_S
        and elapsed[i] - last_steep[i] >= DECOUPLING_FLAT_HISTORY_S
        and hr[i] is not None
        and speed[i] is not None
        and (g := a.grade[i]) is not None
        and abs(g) <= DECOUPLING_FLAT_GRADE_PCT
        and abs((speed[i] or 0.0) - typical) <= DECOUPLING_SPEED_TOLERANCE * typical
    ]
    total = sum(a.weights[i] for i in qualifying)
    if total < DECOUPLING_MIN_SECONDS:
        return skipped(
            f"only {total:.0f} s of flat (|grade| <= {DECOUPLING_FLAT_GRADE_PCT:g} % for the "
            f"preceding {DECOUPLING_FLAT_HISTORY_S:.0f} s too), steadily "
            f"paced (within {DECOUPLING_SPEED_TOLERANCE:.0%} of the median moving speed) moving "
            f"data after the first {DECOUPLING_WARMUP_S:.0f} s; at least "
            f"{DECOUPLING_MIN_SECONDS:.0f} s are needed",
            total,
        )
    running, split = 0.0, len(qualifying)
    for position, i in enumerate(qualifying):
        running += a.weights[i]
        if running >= total / 2:
            split = position + 1
            break
    halves = (qualifying[:split], qualifying[split:])
    hr_means = [_weighted_mean(hr, a.weights, half) for half in halves]
    speed_means = [_weighted_mean(speed, a.weights, half) for half in halves]
    ratios = [
        h / s if h is not None and s else None for h, s in zip(hr_means, speed_means, strict=True)
    ]
    decoupling = (ratios[1] / ratios[0] - 1) * 100 if ratios[0] and ratios[1] is not None else None
    return CardiacDrift(
        status="computed",
        qualifying_seconds=_round(total, 0),
        first_half_average_hr_bpm=_round(hr_means[0]),
        second_half_average_hr_bpm=_round(hr_means[1]),
        first_half_average_speed_mps=_round(speed_means[0], 3),
        second_half_average_speed_mps=_round(speed_means[1], 3),
        first_half_hr_per_speed=_round(ratios[0], 2),
        second_half_hr_per_speed=_round(ratios[1], 2),
        decoupling_percent=_round(decoupling),
    )


def hr_vs_grade(a: Analysis) -> HrGradeRelation:
    def skipped(reason: str, count: int | None = None) -> HrGradeRelation:
        return HrGradeRelation(status="skipped", reason=reason, sample_count=count)

    if not a.series.has("hr_bpm"):
        return skipped("no heart rate was recorded")
    if a.grade is None:
        return skipped("grade needs both distance and elevation")
    grade = Signal(a.elapsed, a.grade)
    start, end = a.elapsed[0], a.elapsed[-1]
    grid = [start + k * HR_GRADE_GRID_S for k in range(int((end - start) // HR_GRADE_GRID_S) + 1)]
    best: tuple[float, int, list[float], list[float]] | None = None
    most = 0
    for lag in HR_GRADE_LAGS_S:
        xs, ys = [], []
        for t in grade_grid(grid, grade):
            g, h = grade.at(t), a.hr.at(t + lag)
            if g is not None and h is not None:
                xs.append(g)
                ys.append(h)
        most = max(most, len(xs))
        if len(xs) < HR_GRADE_MIN_POINTS:
            continue
        r = _pearson(xs, ys)
        if r is not None and (best is None or r > best[0]):
            best = (r, lag, xs, ys)
    if best is None:
        return skipped(
            f"fewer than {HR_GRADE_MIN_POINTS} moving {HR_GRADE_GRID_S} s points with both HR and "
            "grade, or no variation in either",
            most,
        )
    r, lag, xs, ys = best
    mean_x = fmean(xs)
    spread = math.sqrt(fmean((x - mean_x) ** 2 for x in xs))
    if spread < HR_GRADE_MIN_SPREAD_PCT:
        return skipped(
            f"grade barely varies (standard deviation {spread:.1f} %), so no HR-grade "
            "relationship can be estimated",
            len(xs),
        )
    mean_y = fmean(ys)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sum(
        (x - mean_x) ** 2 for x in xs
    )
    return HrGradeRelation(
        status="computed",
        bpm_per_grade_pct=_round(slope, 2),
        hr_at_zero_grade_bpm=_round(mean_y - slope * mean_x),
        correlation=_round(r, 2),
        hr_lag_s=lag,
        sample_count=len(xs),
        grid_seconds=HR_GRADE_GRID_S,
        note=(
            f"The best lag is at the {HR_GRADE_LAGS_S[-1]} s search limit: HR may be following "
            "a slower trend (warm-up, drift) as much as the grade."
            if lag == HR_GRADE_LAGS_S[-1]
            else None
        ),
    )


def grade_grid(grid: Sequence[float], grade: Signal) -> Iterable[float]:
    """Grid times inside the grade signal (grade exists only for moving samples)."""
    if not grade.x:
        return []
    lo, hi = grade.x[0], grade.x[-1]
    return (t for t in grid if lo <= t <= hi)


# --- Matching one segment across activities ---------------------------------------------------


@dataclass
class WindowMatch:
    start_m: float
    end_m: float
    method: str
    confidence: float | None = None
    correlation: float | None = None
    range_ratio: float | None = None
    offset_m: float | None = None
    scale: float | None = None


def _window_profile(
    profile: ElevationProfile, start: float, length: float, scale: float
) -> list[float] | None:
    points = []
    for k in range(int(length // PROFILE_STEP_M) + 1):
        value = profile.at(start + k * PROFILE_STEP_M * scale)
        if value is None:
            return None
        points.append(value)
    return points


def match_window(
    reference: ElevationProfile, start: float, end: float, candidate: ElevationProfile
) -> WindowMatch | str:
    """Find ``[start, end]`` of the reference in the candidate, or say why it cannot."""
    length = end - start
    ref = _window_profile(reference, start, length, 1.0)
    if ref is None:
        return "the window lies outside the reference activity's distance"
    relief = max(ref) - min(ref)
    if relief < MIN_PROFILE_RELIEF_M:
        if candidate.d[0] <= start and end <= candidate.d[-1]:
            return WindowMatch(start, end, "distance_only", offset_m=0.0, scale=1.0)
        return "the activity does not cover the window's distance"
    search = max(MATCH_SEARCH_M, MATCH_SEARCH_FRACTION * start)
    best: tuple[tuple[float, float, float], WindowMatch] | None = None
    steps = int(search // PROFILE_STEP_M)
    for k in range(-steps, steps + 1):
        offset = k * PROFILE_STEP_M
        s = start + offset
        for scale in MATCH_LENGTH_SCALES:
            if s < candidate.d[0] or s + length * scale > candidate.d[-1]:
                continue
            points = _window_profile(candidate, s, length, scale)
            if points is None:
                continue
            r = _pearson(ref, points)
            if r is None:
                continue
            cand_relief = max(points) - min(points)
            ratio = min(relief, cand_relief) / max(relief, cand_relief)
            confidence = max(0.0, r) * ratio
            rank = (confidence, -abs(offset), -abs(scale - 1))
            if best is None or rank > best[0]:
                best = (
                    rank,
                    WindowMatch(
                        start_m=s,
                        end_m=s + length * scale,
                        method="elevation_shape",
                        confidence=_round(confidence, 2),
                        correlation=_round(r, 3),
                        range_ratio=_round(ratio, 2),
                        offset_m=offset,
                        scale=scale,
                    ),
                )
    if best is None:
        return "the activity does not cover a window of this length near the same distance"
    match = best[1]
    if (match.confidence or 0.0) < MIN_MATCH_CONFIDENCE:
        return (
            f"best elevation-shape match has confidence {match.confidence} (correlation "
            f"{match.correlation}, elevation-range ratio {match.range_ratio}) at "
            f"{match.offset_m:+.0f} m, below the {MIN_MATCH_CONFIDENCE} threshold"
        )
    return match


def span_for_window(a: Analysis, start_m: float, end_m: float) -> Span | None:
    """Samples between two distances, classified by the window's average grade."""
    indices = window_indices(a, start_m, end_m)
    if len(indices) < 2:
        return None
    grade = None
    if a.profile is not None:
        top, bottom = a.profile.at(end_m), a.profile.at(start_m)
        if top is not None and bottom is not None and end_m > start_m:
            grade = (top - bottom) / (end_m - start_m) * 100
    return Span(_kind_from_grade(grade), indices[0], indices[-1])
