"""Every timestamp is ISO 8601 with an explicit offset, built from Garmin's true GMT value.

The autouse fixture in conftest.py runs these in Europe/Berlin, which switches from CEST
(+02:00) to CET (+01:00) on 2026-10-25 and back on 2026-03-29.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import pytest

from garmin_owl.normalize import (
    local_iso,
    normalize_activity,
    normalize_activity_detail,
    normalize_body_composition,
    normalize_hrv,
    normalize_sleep,
    normalize_stress,
    normalize_training_readiness,
)

HOUR_MS = 3_600_000


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1000)


def _sleep(start_gmt: int, end_gmt: int, offsets_h: tuple[int, int] | None) -> dict[str, object]:
    dto: dict[str, object] = {
        "sleepStartTimestampGMT": start_gmt,
        "sleepEndTimestampGMT": end_gmt,
    }
    if offsets_h is not None:
        dto["sleepStartTimestampLocal"] = start_gmt + offsets_h[0] * HOUR_MS
        dto["sleepEndTimestampLocal"] = end_gmt + offsets_h[1] * HOUR_MS
    return {"dailySleepDTO": dto}


def test_reported_bedtime_is_not_shifted_by_the_utc_offset_twice() -> None:
    # The live payload behind the misreading: Local is GMT + 2 h, encoded as if it were UTC.
    local_epoch = 1791587896000
    raw = {
        "dailySleepDTO": {
            "sleepStartTimestampGMT": local_epoch - 2 * HOUR_MS,
            "sleepStartTimestampLocal": local_epoch,
        }
    }
    sleep = normalize_sleep(raw, "2026-10-10")
    assert sleep.sleep_start == "2026-10-09T23:18:16+02:00"
    assert datetime.fromisoformat(sleep.sleep_start) == datetime(
        2026, 10, 9, 21, 18, 16, tzinfo=UTC
    )


@pytest.mark.parametrize("with_local", [True, False], ids=["garmin-offsets", "gmt-only"])
def test_autumn_dst_night_keeps_each_end_in_its_own_offset(with_local: bool) -> None:
    # 23:00 CEST on 24 Oct to 07:00 CET on 25 Oct: nine hours, not eight.
    start, end = _ms(2026, 10, 24, 21, 0), _ms(2026, 10, 25, 6, 0)
    sleep = normalize_sleep(_sleep(start, end, (2, 1) if with_local else None), "2026-10-25")
    assert sleep.sleep_start == "2026-10-24T23:00:00+02:00"
    assert sleep.sleep_end == "2026-10-25T07:00:00+01:00"
    assert sleep.sleep_start is not None and sleep.sleep_end is not None
    elapsed = datetime.fromisoformat(sleep.sleep_end) - datetime.fromisoformat(sleep.sleep_start)
    assert elapsed.total_seconds() == 9 * 3600


@pytest.mark.parametrize("with_local", [True, False], ids=["garmin-offsets", "gmt-only"])
def test_spring_dst_night_keeps_each_end_in_its_own_offset(with_local: bool) -> None:
    # 23:00 CET on 28 Mar to 07:00 CEST on 29 Mar: seven hours, not eight.
    start, end = _ms(2026, 3, 28, 22, 0), _ms(2026, 3, 29, 5, 0)
    sleep = normalize_sleep(_sleep(start, end, (1, 2) if with_local else None), "2026-03-29")
    assert sleep.sleep_start == "2026-03-28T23:00:00+01:00"
    assert sleep.sleep_end == "2026-03-29T07:00:00+02:00"


def test_repeated_wall_hour_at_autumn_transition_stays_unambiguous() -> None:
    # 02:30 happens twice on 25 Oct; the offset tells the two readings apart.
    raw = {"stressValuesArray": [[_ms(2026, 10, 25, 0, 30), 20], [_ms(2026, 10, 25, 1, 30), 30]]}
    stress = normalize_stress(raw, "2026-10-25", include_timeseries=True)
    assert stress.timeseries is not None
    assert [point.timestamp for point in stress.timeseries] == [
        "2026-10-25T02:30:00+02:00",
        "2026-10-25T02:30:00+01:00",
    ]


def test_garmin_recorded_offset_wins_while_travelling() -> None:
    # Recorded in New York (-04:00) while this computer is set to Berlin.
    start = _ms(2026, 10, 10, 3, 0)
    sleep = normalize_sleep(_sleep(start, start + 8 * HOUR_MS, (-4, -4)), "2026-10-10")
    assert sleep.sleep_start == "2026-10-09T23:00:00-04:00"


def test_gmt_only_values_use_this_computers_time_zone() -> None:
    previous = os.environ["TZ"]
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        assert local_iso(_ms(2026, 10, 10, 3, 0)) == "2026-10-09T23:00:00-04:00"
    finally:
        os.environ["TZ"] = previous
        time.tzset()


def test_inconsistent_local_value_falls_back_to_the_computers_zone() -> None:
    gmt = _ms(2026, 10, 10, 3, 0)
    assert local_iso(gmt, gmt + 7 * 60_000) == "2026-10-10T05:00:00+02:00"
    assert local_iso(gmt, gmt + 20 * HOUR_MS) == "2026-10-10T05:00:00+02:00"


def test_local_only_value_gets_the_offset_valid_at_that_wall_time() -> None:
    assert local_iso(local="2026-10-24 23:00:00") == "2026-10-24T23:00:00+02:00"
    assert local_iso(local="2026-10-25 07:00:00") == "2026-10-25T07:00:00+01:00"


def test_unparseable_and_missing_values_are_not_invented() -> None:
    assert local_iso() is None
    assert local_iso("not a time") is None
    assert normalize_sleep({"dailySleepDTO": {}}, "2026-10-10").sleep_start is None


def test_readiness_timestamp_is_gmt_rendered_with_offset() -> None:
    raw = [
        {
            "score": 80,
            "timestamp": "2026-10-10T05:00:29.0",
            "timestampLocal": "2026-10-10T07:00:29.0",
        }
    ]
    assert normalize_training_readiness(raw, "2026-10-10").timestamp == "2026-10-10T07:00:29+02:00"


def test_activity_and_lap_start_times_carry_offsets() -> None:
    raw = {
        "activityId": 1,
        "startTimeGMT": "2026-10-25 06:30:00",
        "startTimeLocal": "2026-10-25 07:30:00",
    }
    assert normalize_activity(raw).start_time == "2026-10-25T07:30:00+01:00"
    detail = normalize_activity_detail(
        raw,
        [{"lapIndex": 1, "startTimeGMT": "2026-10-25T06:30:00.0"}],
    )
    assert detail.laps[0].start_time == "2026-10-25T07:30:00+01:00"
    # The listing date (first ten characters) is the local calendar day.
    late = {
        "activityId": 2,
        "startTimeGMT": "2026-10-09 22:30:00",
        "startTimeLocal": "2026-10-10 00:30:00",
    }
    assert (normalize_activity(late).start_time or "")[:10] == "2026-10-10"


def test_hrv_readings_with_garmin_reading_times_are_kept() -> None:
    raw = {
        "hrvReadings": [
            {
                "hrvValue": 42,
                "readingTimeGMT": "2026-10-09T22:05:00.0",
                "readingTimeLocal": "2026-10-10T00:05:00.0",
            }
        ]
    }
    readings = normalize_hrv(raw, "2026-10-10", include_timeseries=True).timeseries
    assert readings is not None
    assert [(point.timestamp, point.value) for point in readings] == [
        ("2026-10-10T00:05:00+02:00", 42)
    ]


def test_weigh_in_uses_gmt_instant_and_keeps_its_local_date() -> None:
    gmt = _ms(2026, 10, 9, 22, 30)
    raw = {"dateWeightList": [{"date": gmt + 2 * HOUR_MS, "timestampGMT": gmt, "weight": 70000}]}
    entry = normalize_body_composition(raw)[0]
    assert entry.timestamp == "2026-10-10T00:30:00+02:00"
