"""On-demand per-activity sample analysis: get_activity_timeseries, get_activity_segments and
compare_activity_segments.

Every Garmin response here is synthetic. ``simulate`` builds a chart response in the exact
shape Garmin's activity details read returns (descriptor list plus metric rows, smart or
1-second recording), including latitude/longitude columns and a geoPolylineDTO filled with
fabricated coordinates, so the tests prove those never reach an output or the cache.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from garmin_owl import activity_series as samples
from garmin_owl.client import GarminDataClient
from garmin_owl.database import GarminDatabase
from garmin_owl.tools import GarminOwlInputError, GarminTools

# Fabricated, distinctive values; searching for them proves no location survives anywhere.
FAKE_LAT, FAKE_LON = 12.3456789, 98.7654321
START_MS = 1_800_000_000_000.0

WALK_KEYS = (
    "directFractionalCadence",
    "sumDistance",
    "sumElapsedDuration",
    "directDoubleCadence",
    "directHeartRate",
    "sumDuration",
    "directBodyBattery",
    "directTimestamp",
    "sumMovingDuration",
    "directSpeed",
    "directElevation",
    "directVerticalSpeed",
    "directLatitude",
    "directLongitude",
)
# The indoor "Cardio" shape: distance and moving time are present but always zero.
INDOOR_KEYS = (
    "sumDistance",
    "sumMovingDuration",
    "directTimestamp",
    "sumDuration",
    "directHeartRate",
    "directBodyBattery",
    "sumElapsedDuration",
    "directRespirationRate",
)


def loop_profile(distance: float) -> float:
    """Flat 500 m, a 300 m climb at 8 % (24 m), then flat."""
    return 340.0 + 0.08 * min(max(distance - 500.0, 0.0), 300.0)


def simulate(
    *,
    profile: Callable[[float], float] = loop_profile,
    length_m: float = 1900.0,
    speed: float = 1.25,
    intervals: tuple[float, ...] = (1.0,),
    stop: tuple[float, float] | None = None,
    pause: tuple[float, float] | None = None,
    noise_m: float = 0.0,
    hr_delay_s: float = 20.0,
    hr_tau_s: float = 25.0,
    seed: int = 0,
    keys: tuple[str, ...] = WALK_KEYS,
    shuffle_columns: bool = False,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    """A Garmin-shaped chart response for a walk along ``profile``.

    HR follows grade through a pure delay plus a first-order lag, like a real heart. ``stop``
    is (start_s, duration) standing still with the timer running; ``pause`` is a stopped timer
    with no samples recorded.
    """
    rng = random.Random(seed)
    t = distance = moving = timer = 0.0
    hr = 92.0
    history: list[tuple[float, float]] = []
    rows: list[dict[str, float | None]] = []
    step = 0
    while distance < length_m and (max_seconds is None or t <= max_seconds):
        stopped = stop is not None and stop[0] <= t < stop[0] + stop[1]
        grade = (profile(distance + 1) - profile(distance)) * 100
        history.append((t, 0.0 if stopped else grade))
        delayed = next((g for s, g in reversed(history) if s <= t - hr_delay_s), 0.0)
        target = (85.0 if stopped else 95.0) + 4.0 * delayed
        rows.append(
            {
                "sumElapsedDuration": t,
                "sumDuration": timer,
                "sumMovingDuration": moving,
                "directTimestamp": START_MS + t * 1000,
                "sumDistance": distance,
                "directSpeed": 0.0 if stopped else speed,
                "directElevation": profile(distance) + rng.gauss(0, noise_m),
                "directHeartRate": round(hr),
                "directDoubleCadence": 0.0 if stopped else 110.0,
                "directFractionalCadence": 0.0,
                "directBodyBattery": 70.0,
                "directVerticalSpeed": 0.0,
                "directLatitude": FAKE_LAT,
                "directLongitude": FAKE_LON,
                "directRespirationRate": 24.0 + delayed,
            }
        )
        dt = intervals[step % len(intervals)]
        step += 1
        hr += (target - hr) * (1 - math.exp(-dt / hr_tau_s))
        if pause is not None and pause[0] <= t < pause[0] + dt:
            t += pause[1]  # watch paused: no samples, the timer does not advance
        t += dt
        timer += dt
        if not stopped:
            distance += speed * dt
            moving += dt
    order = list(keys)
    if shuffle_columns:
        random.Random(seed + 1).shuffle(order)
    descriptors = [
        {"metricsIndex": i, "key": key, "unit": {"factor": 1.0}} for i, key in enumerate(order)
    ]
    if "sumDistance" in keys and "directElevation" not in keys:
        for row in rows:
            row["sumDistance"] = 0.0
            row["sumMovingDuration"] = 0.0
    return {
        "activityId": 1,
        "measurementCount": len(order),
        "metricsCount": len(rows),
        "totalMetricsCount": len(rows),
        "metricDescriptors": descriptors,
        "activityDetailMetrics": [{"metrics": [row.get(key) for key in order]} for row in rows],
        "geoPolylineDTO": {
            "startPoint": {"lat": FAKE_LAT, "lon": FAKE_LON},
            "endPoint": {"lat": FAKE_LAT, "lon": FAKE_LON},
            "minLat": FAKE_LAT,
            "maxLat": FAKE_LAT,
            "minLon": FAKE_LON,
            "maxLon": FAKE_LON,
            "polyline": [{"lat": FAKE_LAT, "lon": FAKE_LON}],
        },
        "heartRateDTOs": None,
        "pendingData": None,
        "detailsAvailable": True,
    }


def indoor(seconds: float = 1800.0) -> dict[str, Any]:
    """An indoor "Cardio" session: HR and respiration only, zeros for distance."""
    return simulate(keys=INDOOR_KEYS, intervals=(2.0,), max_seconds=seconds, length_m=1e9)


class FakeGarmin:
    """Answers only the reads these tools may make, and records each one."""

    def __init__(self, charts: dict[int, dict[str, Any]]) -> None:
        self.charts = charts
        self.calls: list[str] = []
        self.chart_arguments: list[tuple[str, int, int]] = []

    def get_activity_details(
        self, activity_id: str, maxchart: int = 2000, maxpoly: int = 4000
    ) -> dict[str, Any]:
        self.calls.append("get_activity_details")
        self.chart_arguments.append((activity_id, maxchart, maxpoly))
        return self.charts[int(activity_id)]

    def get_activity(self, activity_id: str) -> dict[str, Any]:
        self.calls.append("get_activity")
        return {
            "activityId": int(activity_id),
            "activityName": "Loop",
            "activityTypeDTO": {"typeKey": "walking"},
            "summaryDTO": {
                "startTimeGMT": "2026-11-15T09:00:00.0",
                "startTimeLocal": "2026-11-15T10:00:00.0",
            },
        }

    def get_activity_hr_in_timezones(self, activity_id: str) -> list[Any]:
        self.calls.append("get_activity_hr_in_timezones")
        return []

    def get_activity_power_in_timezones(self, activity_id: str) -> list[Any]:
        self.calls.append("get_activity_power_in_timezones")
        return []


def make_tools(
    charts: dict[int, dict[str, Any]], database: GarminDatabase | None = None
) -> tuple[GarminTools, FakeGarmin]:
    fake = FakeGarmin(charts)
    return GarminTools(GarminDataClient(fake), database), fake  # type: ignore[arg-type]


def analysis_of(chart: dict[str, Any]) -> samples.Analysis:
    series = samples.parse_activity_chart(chart)
    assert series is not None
    return samples.analyse(series)


def all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(all_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(all_keys(v) for v in value))
    return set()


# --- Parsing and location -----------------------------------------------------------------


def test_location_key_detection() -> None:
    for key in (
        "directLatitude",
        "directLongitude",
        "geoPolylineDTO",
        "startPoint",
        "minLat",
        "maxLon",
        "polyline",
        "position_lat",
        "start_lng",
    ):
        assert samples.is_location_key(key), key
    for key in (
        "correlation",
        "elevation_m",
        "calculated",
        "elapsed_s",
        "sumDistance",
        "plateau",
        "sample_count",
        "relation",
    ):
        assert not samples.is_location_key(key), key


def test_columns_are_mapped_by_descriptor_key_not_position() -> None:
    plain = samples.parse_activity_chart(simulate())
    shuffled = samples.parse_activity_chart(simulate(shuffle_columns=True))
    assert plain is not None and shuffled is not None
    assert plain.channels == shuffled.channels
    assert plain.elapsed_s == shuffled.elapsed_s


def test_chart_read_requests_no_polyline() -> None:
    tools, fake = make_tools({7: simulate()})
    tools.get_activity_timeseries(7)
    assert fake.chart_arguments == [("7", 10_000, 0)]


def test_unusable_chart_is_missing_data_not_a_crash() -> None:
    assert (
        samples.parse_activity_chart({"metricDescriptors": [], "activityDetailMetrics": []}) is None
    )
    assert samples.parse_activity_chart(None) is None
    tools, _ = make_tools({7: {"metricDescriptors": [], "activityDetailMetrics": []}})
    with pytest.raises(Exception, match="no per-sample data"):
        tools.get_activity_timeseries(7)


def test_no_location_in_any_output_or_the_cache(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    charts = {1: simulate(), 2: simulate(seed=3), 3: indoor()}
    tools, _ = make_tools(charts, database)
    outputs = [
        tools.get_activity_timeseries(1),
        tools.get_activity_timeseries(3),
        tools.get_activity_segments(1),
        tools.get_activity_segments(3),
        tools.compare_activity_segments([1, 2, 3]),
    ]
    for output in outputs:
        assert not [key for key in all_keys(output) if samples.is_location_key(key)]
        text = json.dumps(output)
        assert str(FAKE_LAT)[:7] not in text and str(FAKE_LON)[:7] not in text

    with sqlite3.connect(database.path) as connection:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            assert not [c for c in columns if samples.is_location_key(c)], table
            for row in connection.execute(f"SELECT * FROM {table}"):
                for value in row:
                    if isinstance(value, float):
                        assert abs(value - FAKE_LAT) > 1e-3 and abs(value - FAKE_LON) > 1e-3, table
                    if isinstance(value, str):
                        assert str(FAKE_LAT)[:7] not in value, table


# --- Signal processing --------------------------------------------------------------------


def test_known_climb_is_detected_with_grade_and_hr_lag() -> None:
    a = analysis_of(simulate())
    spans = samples.DistanceSegmenter(5).split(a)
    climbs = [samples.segment_metrics(a, s) for s in spans if s.kind == "climb"]
    assert len(climbs) == 1
    climb = climbs[0]
    assert climb.start_distance_m is not None and abs(climb.start_distance_m - 500) <= 30
    assert climb.length_m is not None and abs(climb.length_m - 300) <= 40
    assert climb.elevation_gain_m is not None and abs(climb.elevation_gain_m - 24) <= 2
    assert climb.average_grade_pct is not None and abs(climb.average_grade_pct - 8) <= 1.5
    assert climb.average_cadence_spm == 110
    assert climb.pace_s_per_km == 800
    assert climb.peak_hr_bpm is not None and climb.hr_at_start_bpm is not None
    assert climb.peak_hr_bpm - climb.hr_at_start_bpm > 20

    relation = samples.hr_vs_grade(a)
    assert relation.status == "computed"
    assert relation.bpm_per_grade_pct is not None and relation.bpm_per_grade_pct > 1
    assert relation.hr_lag_s is not None and relation.hr_lag_s >= 15
    assert relation.sample_count is not None and relation.sample_count > 200


def test_hr_peak_lag_and_recovery_after_a_short_climb() -> None:
    def short_climb(distance: float) -> float:
        return 340.0 + 0.08 * min(max(distance - 500.0, 0.0), 100.0)  # 8 m in 80 s

    a = analysis_of(simulate(profile=short_climb, hr_delay_s=20, hr_tau_s=40))
    spans = [s for s in samples.DistanceSegmenter(5).split(a) if s.kind == "climb"]
    assert len(spans) == 1
    climb = samples.segment_metrics(a, spans[0])
    # HR trails the effort: it peaks after the climb ends, then recovers.
    assert climb.hr_peak_after_end_s is not None and climb.hr_peak_after_end_s >= 10
    assert climb.hr_peak_lag_s is not None and climb.hr_peak_lag_s > climb.duration_s
    assert climb.hr_drop_60s_after_end_bpm is not None and climb.hr_drop_60s_after_end_bpm > 0


def test_noisy_elevation_is_smoothed_before_grade() -> None:
    chart = simulate(noise_m=0.4, seed=4, intervals=(4.0, 5.0, 4.5))
    rows = chart["activityDetailMetrics"]
    elevation_index = WALK_KEYS.index("directElevation")
    for row in rows[40::97]:  # sparse barometric spikes
        row["metrics"][elevation_index] += 6.0
    a = analysis_of(chart)
    spans = samples.DistanceSegmenter(5).split(a)
    assert [s.kind for s in spans].count("climb") == 1
    assert "descent" not in [s.kind for s in spans]
    distance = a.channel("distance_m")
    assert a.grade is not None
    flat = [
        g
        for d, g in zip(distance, a.grade, strict=True)
        if g is not None and d is not None and 1000 < d < 1800
    ]
    # Sample-to-sample grade on this data exceeds 100 %; smoothed grade stays near zero.
    assert flat and max(abs(g) for g in flat) < 2.5
    assert abs(sum(flat) / len(flat)) < 0.2


def test_pause_and_stop_are_reported_and_excluded_from_grade() -> None:
    chart = simulate(stop=(300.0, 60.0), pause=(900.0, 120.0))
    a = analysis_of(chart)
    quality = samples.sampling_quality(a)
    kinds = {stretch.kind: stretch for stretch in quality.stretches}
    assert abs(kinds["stopped"].duration_s - 60) <= 2
    assert abs(kinds["timer_paused"].duration_s - 121) <= 2
    assert quality.recording == "every_second"
    assert a.moving is not None and a.grade is not None
    during_stop = [i for i, t in enumerate(a.elapsed) if 305 <= t <= 355]
    assert all(not a.moving[i] and a.grade[i] is None for i in during_stop)

    tools, _ = make_tools({1: chart})
    result = tools.get_activity_timeseries(1, resample_seconds=10)
    moving = result["columns"].index("moving")
    elapsed = result["columns"].index("elapsed_s")
    pace = result["columns"].index("pace_s_per_km")
    stopped_rows = [row for row in result["rows"] if 310 <= row[elapsed] <= 350]
    assert stopped_rows and all(row[moving] is False and row[pace] is None for row in stopped_rows)
    # The paused stretch has no samples, so no rows: gaps stay visible, not interpolated.
    assert not [row for row in result["rows"] if 925 <= row[elapsed] <= 1010]


def test_smart_recording_is_identified() -> None:
    quality = samples.sampling_quality(analysis_of(simulate(intervals=(4.0, 5.0, 4.5))))
    assert quality.recording == "smart_recording"
    assert quality.median_interval_s == 4.5 and quality.max_interval_s == 5.0


def test_cardiac_drift_needs_enough_flat_steady_data() -> None:
    short = samples.cardiac_drift(analysis_of(simulate(length_m=1200)))
    assert short.status == "skipped" and short.reason and "600 s" in short.reason

    def flat(_: float) -> float:
        return 350.0

    long_flat = samples.cardiac_drift(analysis_of(simulate(profile=flat, length_m=3000)))
    assert long_flat.status == "computed"
    assert long_flat.decoupling_percent is not None and abs(long_flat.decoupling_percent) < 3


# --- Tools ---------------------------------------------------------------------------------


def test_timeseries_columns_and_derived_notices() -> None:
    tools, _ = make_tools({1: simulate()})
    result = tools.get_activity_timeseries(1)
    assert result["columns"] == [
        "elapsed_s",
        "moving",
        "distance_m",
        "hr_bpm",
        "speed_mps",
        "pace_s_per_km",
        "cadence_spm",
        "elevation_m",
        "grade_pct",
    ]
    assert result["start_time"] == "2026-11-15T10:00:00+01:00"
    assert result["activity_type"] == "walking"
    assert all(len(row) == len(result["columns"]) for row in result["rows"])
    statuses = {(n["field"], n["status"]) for n in result["availability"]}
    assert ("grade_pct", "garmin_owl_derived") in statuses
    assert ("elevation_m", "garmin_owl_derived") in statuses


def test_respiration_absent_on_walk_is_omitted_with_notice() -> None:
    tools, _ = make_tools({1: simulate()})
    result = tools.get_activity_timeseries(1)
    assert "respiration_brpm" not in result["columns"]
    assert {"field": "respiration_brpm", "status": "not_recorded"}.items() <= next(
        n for n in result["availability"] if n["field"] == "respiration_brpm"
    ).items()
    assert "respiration_brpm" in result["sampling"]["not_recorded_metrics"]


def test_indoor_activity_degrades_gracefully() -> None:
    tools, _ = make_tools({3: indoor()})
    series = tools.get_activity_timeseries(3)
    assert series["columns"] == ["elapsed_s", "hr_bpm", "respiration_brpm"]
    respiration = series["columns"].index("respiration_brpm")
    assert all(row[respiration] is not None and row[respiration] > 0 for row in series["rows"])
    missing = {n["field"] for n in series["availability"] if n["status"] == "not_recorded"}
    assert {"distance_m", "elevation_m", "speed_mps"} <= missing
    assert "respiration_brpm" not in missing

    segments = tools.get_activity_segments(3)
    assert segments["segments"] == [] and segments["segment_count"] == 0
    assert any(n["field"] == "segments" for n in segments["availability"])
    assert segments["cardiac_drift"]["status"] == "skipped"
    assert segments["hr_vs_grade"]["status"] == "skipped"

    with pytest.raises(GarminOwlInputError, match="no distance"):
        tools.get_activity_timeseries(3, start_distance_m=100)


def test_respiration_present_is_returned_per_segment() -> None:
    chart = simulate(keys=(*WALK_KEYS, "directRespirationRate"))
    tools, _ = make_tools({1: chart})
    assert "respiration_brpm" in tools.get_activity_timeseries(1)["columns"]
    climb = next(s for s in tools.get_activity_segments(1)["segments"] if s["kind"] == "climb")
    assert climb["peak_respiration_brpm"] > climb["average_respiration_brpm"] > 24


def test_row_cap_coarsens_and_discloses() -> None:
    def flat(_: float) -> float:
        return 350.0

    chart = simulate(profile=flat, length_m=13_500)  # 3 hours of 1-second samples
    tools, _ = make_tools({1: chart})
    result = tools.get_activity_timeseries(1, resample_seconds=1)
    assert result["row_count"] <= samples.MAX_TIMESERIES_ROWS
    assert result["resample_seconds"] > 1 and result["requested_resample_seconds"] == 1
    assert any(n["status"] == "coarsened" for n in result["availability"])
    assert len(json.dumps(result, separators=(",", ":"))) < 40_000


def test_metric_selection_and_distance_window() -> None:
    tools, _ = make_tools({1: simulate()})
    result = tools.get_activity_timeseries(
        1,
        metrics=["hr_bpm", "grade_pct", "respiration_brpm"],
        start_distance_m=450,
        end_distance_m=900,
    )
    assert result["columns"] == ["elapsed_s", "hr_bpm", "grade_pct"]
    assert result["start_distance_m"] == 450
    with pytest.raises(GarminOwlInputError, match="metrics"):
        tools.get_activity_timeseries(1, metrics=["latitude"])
    with pytest.raises(GarminOwlInputError, match="less than"):
        tools.get_activity_timeseries(1, start_distance_m=900, end_distance_m=100)
    with pytest.raises(GarminOwlInputError, match="No samples"):
        tools.get_activity_timeseries(1, start_distance_m=5000, end_distance_m=6000)
    with pytest.raises(GarminOwlInputError, match="resample_seconds"):
        tools.get_activity_timeseries(1, resample_seconds=0)


def test_reads_are_cached_and_cache_answers_like_live(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    tools, fake = make_tools({1: simulate(stop=(300.0, 30.0))}, database)
    live_series = tools.get_activity_timeseries(1)
    live_segments = tools.get_activity_segments(1)
    # One chart read and one summary read for the start time's UTC offset; then nothing.
    assert fake.calls == ["get_activity_details", "get_activity"]
    assert tools.get_activity_timeseries(1) == live_series
    assert tools.get_activity_segments(1) == live_segments
    assert fake.calls == ["get_activity_details", "get_activity"]


def test_cached_activity_summary_avoids_the_extra_read(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    tools, fake = make_tools({1: simulate()}, database)
    tools.get_activity(1)
    fake.calls.clear()
    tools.get_activity_timeseries(1)
    assert fake.calls == ["get_activity_details"]


def test_existing_activity_tools_never_read_samples() -> None:
    tools, fake = make_tools({1: simulate(), 2: simulate()})
    tools.get_activity(1)
    tools.get_activities_detail([1, 2])
    tools.compare_activities([1, 2])
    assert "get_activity_details" not in fake.calls


# --- Comparison ----------------------------------------------------------------------------


def test_same_climb_is_matched_across_activities_without_gps() -> None:
    def shifted(distance: float) -> float:
        # The same climb, reached 60 m later and measured 3 % longer by GPS on another day.
        return 338.5 + 0.08 / 1.03 * min(max(distance - 560.0, 0.0), 309.0)

    def other_route(distance: float) -> float:
        return 340.0 + 3.0 * math.sin(distance / 40.0)

    charts = {
        1: simulate(noise_m=0.3, seed=1),
        2: simulate(profile=shifted, noise_m=0.3, seed=2, intervals=(4.0, 5.0)),
        3: simulate(profile=other_route, seed=3),
        4: indoor(),
    }
    tools, _ = make_tools(charts)
    result = tools.compare_activity_segments([1, 2, 3, 4])
    assert result["reference_activity_id"] == 1 and result["segment"] == "largest_climb"
    matched = {m["activity_id"]: m for m in result["matched"]}
    assert set(matched) == {1, 2}
    assert matched[1]["reference"] is True
    other = matched[2]
    assert other["match_method"] == "elevation_shape"
    assert other["match_confidence"] >= samples.MIN_MATCH_CONFIDENCE
    assert abs(other["segment"]["start_distance_m"] - 560) <= 40
    assert abs(other["segment"]["elevation_gain_m"] - 24) <= 3
    assert other["segment"]["kind"] == "climb"
    unmatched = {u["activity_id"]: u for u in result["unmatched"]}
    assert unmatched[3]["status"] == "no_match"
    assert unmatched[4]["status"] == "not_matchable" and "no distance" in unmatched[4]["reason"]


def test_distance_window_comparison_and_validation() -> None:
    tools, _ = make_tools({1: simulate(), 2: simulate(seed=5)})
    result = tools.compare_activity_segments(
        [1, 2], "distance_window", reference_activity_id=2, start_distance_m=400, end_distance_m=900
    )
    assert result["reference_activity_id"] == 2
    assert [m["activity_id"] for m in result["matched"]] == [1, 2]
    with pytest.raises(GarminOwlInputError, match="needs start_distance_m"):
        tools.compare_activity_segments([1, 2], "distance_window")
    with pytest.raises(GarminOwlInputError, match="between 2 and 10"):
        tools.compare_activity_segments([1])
    with pytest.raises(GarminOwlInputError, match="reference_activity_id"):
        tools.compare_activity_segments([1, 2], reference_activity_id=9)

    indoor_tools, _ = make_tools({3: indoor(), 1: simulate()})
    with pytest.raises(GarminOwlInputError, match="without GPS"):
        indoor_tools.compare_activity_segments([3, 1])
