from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from garminconnect import GarminConnectConnectionError, GarminConnectNotFoundError

from garmin_owl.client import GarminDataClient
from garmin_owl.database import GarminDatabase
from garmin_owl.models import (
    ActivityDetail,
    ActivitySummary,
    DailySummary,
    HrvSummary,
    SleepSummary,
    TrainingLoad,
    TrainingReadiness,
)
from garmin_owl.tools import GarminTools, parse_date, parse_range, validate_activity_id

DATE = "2026-08-30"


class FakeGarmin:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.activity_arguments: list[str] = []
        self.date_arguments: list[tuple[str, str | None]] = []

    def _record[T](self, name: str, result: T) -> T:
        self.calls.append(name)
        return result

    def get_user_summary(self, cdate: str) -> dict[str, Any]:
        return self._record("get_user_summary", {"totalSteps": 1000, "restingHeartRate": 50})

    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        return self._record("get_sleep_data", {"dailySleepDTO": {"sleepTimeSeconds": 28000}})

    def get_sleep_daily(self, startdate: str, enddate: str) -> list[dict[str, Any]]:
        return self._record(
            "get_sleep_daily",
            [
                {
                    "calendarDate": startdate,
                    "values": {"sleepScore": 80, "skinTempC": 0.2},
                }
            ],
        )

    def get_hrv_data(self, cdate: str) -> dict[str, Any]:
        return self._record("get_hrv_data", {"hrvSummary": {"nightlyAverage": 55}})

    def get_hrv_data_range(self, startdate: str, enddate: str) -> dict[str, Any]:
        return self._record(
            "get_hrv_data_range",
            {"hrvSummaries": [{"calendarDate": startdate, "lastNightAvg": 55}]},
        )

    def get_rhr_daily(self, startdate: str, enddate: str) -> list[dict[str, Any]]:
        return self._record("get_rhr_daily", [{"calendarDate": startdate, "value": 50}])

    def get_training_readiness(self, cdate: str) -> list[dict[str, Any]]:
        return self._record("get_training_readiness", [{"score": 75}])

    def get_body_battery(self, startdate: str, enddate: str | None = None) -> list[dict[str, Any]]:
        values = [[1_700_000_000_000 + i * 60_000, i % 100] for i in range(2000)]
        return self._record(
            "get_body_battery",
            [{"date": startdate, "charged": 45, "bodyBatteryValuesArray": values}],
        )

    def get_stress_data(self, cdate: str) -> dict[str, Any]:
        values = [[1_700_000_000_000 + i * 60_000, i % 90] for i in range(2000)]
        return self._record("get_stress_data", {"avgStressLevel": 25, "stressValuesArray": values})

    def get_activities(self, start: int = 0, limit: int = 20) -> list[dict[str, Any]]:
        return self._record(
            "get_activities",
            [{"activityId": i + 1, "activityName": f"Activity {i + 1}"} for i in range(limit)],
        )

    def get_activities_by_date(
        self, startdate: str, enddate: str | None = None
    ) -> list[dict[str, Any]]:
        self.date_arguments.append((startdate, enddate))
        return self._record("get_activities_by_date", [{"activityId": 1, "activityName": "Run"}])

    def get_activity(self, activity_id: str) -> dict[str, Any]:
        self.activity_arguments.append(activity_id)
        return self._record("get_activity", {"activityId": int(activity_id), "lapDTOs": []})

    def get_activity_hr_in_timezones(self, activity_id: str) -> list[dict[str, Any]]:
        self.activity_arguments.append(activity_id)
        return self._record("get_activity_hr_in_timezones", [])

    def get_activity_power_in_timezones(self, activity_id: str) -> list[dict[str, Any]]:
        self.activity_arguments.append(activity_id)
        return self._record("get_activity_power_in_timezones", [])

    def get_weigh_ins(self, startdate: str, enddate: str) -> dict[str, Any]:
        return self._record(
            "get_weigh_ins", {"dateWeightList": [{"calendarDate": startdate, "weight": 70000}]}
        )

    def get_heart_rate_zones(self) -> list[dict[str, Any]]:
        return self._record("get_heart_rate_zones", [{"sport": "DEFAULT", "zone1Floor": 90}])

    def get_power_zones(self) -> list[dict[str, Any]]:
        return self._record("get_power_zones", [{"sport": "CYCLING", "zone1Floor": 100}])

    def get_running_tolerance(
        self, startdate: str, enddate: str, aggregation: str
    ) -> list[dict[str, Any]]:
        return self._record(
            "get_running_tolerance",
            [{"calendarDate": startdate, "acuteTolerance": 25}],
        )


@pytest.fixture
def fake() -> FakeGarmin:
    return FakeGarmin()


def _client(api: Any) -> GarminDataClient:
    return GarminDataClient(api)


@pytest.fixture
def tools(fake: FakeGarmin) -> GarminTools:
    return GarminTools(_client(fake))


def test_date_validation() -> None:
    assert parse_date(DATE).isoformat() == DATE
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_date("30-08-2026")
    with pytest.raises(ValueError, match="real calendar"):
        parse_date("2026-02-30")
    with pytest.raises(ValueError, match="on or before"):
        parse_range("2026-08-30", "2026-08-01")
    with pytest.raises(ValueError, match="366"):
        parse_range("2025-01-01", "2026-08-30")


def test_activity_id_validation() -> None:
    assert validate_activity_id(1) == 1
    for invalid in (0, -1, True):
        with pytest.raises(ValueError, match="positive"):
            validate_activity_id(invalid)


def test_tools_call_only_explicit_reads(tools: GarminTools, fake: FakeGarmin) -> None:
    tools.get_daily_summary(DATE)
    tools.get_sleep(DATE)
    tools.get_hrv(DATE)
    tools.get_training_readiness(DATE)
    tools.get_body_battery(DATE)
    tools.get_stress(DATE)
    tools.get_activities(limit=2)
    tools.get_activity(123)
    tools.get_body_composition(DATE, DATE)
    tools.get_training_zones()
    tools.get_running_tolerance(7, DATE)
    assert set(fake.calls) == {
        "get_user_summary",
        "get_sleep_data",
        "get_hrv_data",
        "get_training_readiness",
        "get_body_battery",
        "get_stress_data",
        "get_activities_by_date",
        "get_activity",
        "get_activity_hr_in_timezones",
        "get_activity_power_in_timezones",
        "get_weigh_ins",
        "get_heart_rate_zones",
        "get_power_zones",
        "get_running_tolerance",
    }
    assert not any(
        word in name for name in fake.calls for word in ("set", "add", "delete", "upload", "create")
    )
    assert fake.activity_arguments == ["123", "123", "123"]


class WalkingGarmin(FakeGarmin):
    def get_activity(self, activity_id: str) -> dict[str, Any]:
        self.activity_arguments.append(activity_id)
        return self._record(
            "get_activity",
            {
                "activityId": int(activity_id),
                "activityTypeDTO": {"typeKey": "walking"},
                "summaryDTO": {
                    "activityName": "Walk",
                    "duration": 1200,
                    "distance": 1500,
                },
            },
        )

    def get_activity_hr_in_timezones(self, activity_id: str) -> list[dict[str, Any]]:
        self.activity_arguments.append(activity_id)
        raise GarminConnectNotFoundError("unsupported for this walking activity")

    def get_activity_power_in_timezones(self, activity_id: str) -> list[dict[str, Any]]:
        self.activity_arguments.append(activity_id)
        raise GarminConnectNotFoundError("unsupported for this walking activity")


def test_walking_activity_optional_zone_failures_are_non_fatal() -> None:
    fake = WalkingGarmin()
    tools = GarminTools(_client(fake))
    result = tools.get_activity(24_169_622_553)
    assert result["summary"]["activity_id"] == 24_169_622_553
    assert result["summary"]["activity_type"] == "walking"
    assert result["laps"] == []
    assert result["hr_zones_seconds"] == {}
    assert result["power_zones_seconds"] == {}
    assert "training_effect_aerobic" not in result
    assert "training_effect_anaerobic" not in result
    assert fake.activity_arguments == ["24169622553"] * 3


def test_output_stays_compact_with_large_upstream_series(tools: GarminTools) -> None:
    body = tools.get_body_battery(DATE, include_timeseries=True)
    stress = tools.get_stress(DATE, include_timeseries=True)
    assert len(body["timeseries"]) <= 48
    assert len(stress["timeseries"]) <= 48
    assert len(json.dumps(body)) < 5000
    assert len(json.dumps(stress)) < 5000


def test_recovery_has_no_derived_score(tools: GarminTools) -> None:
    result = tools.get_recovery(DATE)
    assert result["training_readiness"]["score"] == 75
    assert "recovery_score" not in result
    assert "Garmin-provided metrics only" in result["note"]


def test_activity_limit_validation(tools: GarminTools) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        tools.get_activities(limit=101)


def test_activity_detail_is_served_from_cache_without_new_calls(
    fake: FakeGarmin, tmp_path: Path
) -> None:
    tools = GarminTools(_client(fake), GarminDatabase(tmp_path / "garmin.sqlite"))
    first = tools.get_activity(123)
    first_calls = list(fake.calls)
    second = tools.get_activity(123)
    assert first == second
    assert fake.calls == first_calls


def test_v2_bound_validation(tools: GarminTools) -> None:
    with pytest.raises(ValueError, match="1 and 90"):
        tools.get_recent_activities(days=91)
    with pytest.raises(ValueError, match="1 and 90"):
        tools.get_running_tolerance(days=91)
    with pytest.raises(ValueError, match="one of 7, 14, or 28"):
        tools.get_recovery_trend(8)
    with pytest.raises(ValueError, match="between 2 and 10"):
        tools.compare_activities([1])
    with pytest.raises(ValueError, match="unique"):
        tools.compare_activities([1, 1])


def test_recovery_trend_compares_requested_date_with_preceding_days(tmp_path: Path) -> None:
    fake = FakeGarmin()
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    tools = GarminTools(_client(fake), database)
    for day in range(1, 8):
        cdate = f"2026-01-{day:02d}"
        current = 100 if day == 7 else 10
        readiness = TrainingReadiness(date=cdate, score=current)
        database.put_daily(DailySummary(date=cdate, resting_hr_bpm=current), readiness)
        database.put_sleep(SleepSummary(date=cdate, sleep_score=current))
        database.put_hrv(HrvSummary(date=cdate, nightly_average_ms=current))
        database.mark_synced("readiness", cdate)

    result = tools._recovery_trend(7, date(2026, 1, 7)).compact()
    metrics = {item["metric"]: item for item in result["metrics"]}
    resting_hr = metrics["resting_hr_bpm"]
    assert resting_hr["current_date"] == "2026-01-07"
    assert resting_hr["current"] == 100
    assert resting_hr["recent_average"] == 10
    assert resting_hr["difference"] == 90
    assert resting_hr["sample_days"] == 6
    assert resting_hr["baseline_start"] == "2026-01-01"
    assert resting_hr["baseline_end"] == "2026-01-06"
    assert fake.calls == ["get_sleep_daily", "get_body_battery"]


def test_training_week_discloses_partial_detail_coverage(tmp_path: Path) -> None:
    fake = FakeGarmin()
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    tools = GarminTools(_client(fake), database)
    database.put_activity_detail(
        ActivityDetail(
            summary=ActivitySummary(
                activity_id=1,
                start_time="2026-01-05 08:00:00",
                duration_seconds=100,
            ),
            training_effect_aerobic=2.0,
            hr_zones_seconds={"zone_2": 80},
        )
    )
    database.put_activity_summary(
        ActivitySummary(
            activity_id=2,
            start_time="2026-01-06 08:00:00",
            duration_seconds=100,
        )
    )
    database.mark_synced("activities", "2026-01-05:2026-01-11")

    result = tools.get_training_week("2026-01-07")
    notices = {item["field"]: item for item in result["availability"]}
    assert result["activity_count"] == 2
    assert result["detail_activity_count"] == 1
    assert result["training_effect_activity_count"] == 1
    assert result["hr_zones_activity_count"] == 1
    assert result["hr_zones_seconds"] == {"zone_2": 80.0}
    assert notices["training_effect"]["status"] == "partial_coverage"
    assert notices["hr_zones_seconds"]["status"] == "partial_coverage"
    assert fake.calls == []


def test_historical_training_context_uses_activities_ending_on_requested_date(
    tmp_path: Path,
) -> None:
    fake = FakeGarmin()
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    tools = GarminTools(_client(fake), database)
    for day in range(1, 8):
        cdate = f"2026-01-{day:02d}"
        readiness = TrainingReadiness(date=cdate, score=70)
        database.put_daily(DailySummary(date=cdate, resting_hr_bpm=50), readiness)
        database.put_sleep(SleepSummary(date=cdate, sleep_score=80))
        database.put_hrv(HrvSummary(date=cdate, nightly_average_ms=55))
        database.mark_synced("readiness", cdate)
    database.put_training_load(TrainingLoad(date="2026-01-07", acute_load=100))
    database.mark_synced("training_load", "2026-01-07")
    database.put_activity_summary(ActivitySummary(activity_id=1, start_time="2026-01-05 08:00:00"))
    database.put_activity_summary(ActivitySummary(activity_id=2, start_time="2026-08-30 08:00:00"))
    database.mark_synced("activities", "2026-01-01:2026-01-07")

    result = tools.get_training_context("2026-01-07")
    assert [item["activity_id"] for item in result["recent_activities"]] == [1]
    assert fake.calls == ["get_sleep_daily", "get_body_battery"]


class CycleGarmin(FakeGarmin):
    def get_menstrual_data_for_date(self, fordate: str) -> dict[str, Any]:
        return self._record(
            "get_menstrual_data_for_date",
            {"daySummary": {"currentPhase": 1, "dayInCycle": 2}},
        )

    def get_menstrual_calendar_data(self, startdate: str, enddate: str) -> dict[str, Any]:
        return self._record("get_menstrual_calendar_data", {"cycleSummaries": []})


def test_cycle_is_on_demand_normalized_and_cached(tmp_path: Path) -> None:
    fake = CycleGarmin()
    tools = GarminTools(_client(fake), GarminDatabase(tmp_path / "garmin.sqlite"))
    first = tools.get_cycle("2026-01-01")
    first_calls = list(fake.calls)
    second = tools.get_cycle("2026-01-01")
    assert first == second
    assert first["phase"] == "menstruation"
    assert (
        fake.calls
        == first_calls
        == [
            "get_menstrual_data_for_date",
            "get_menstrual_calendar_data",
        ]
    )


def test_body_battery_and_stress_keep_their_full_shape_when_cached(tmp_path: Path) -> None:
    """A cache hit must not answer with fewer Garmin fields than a live read."""
    fake = FakeGarmin()
    tools = GarminTools(_client(fake), GarminDatabase(tmp_path / "garmin.sqlite"))
    live_battery = tools.get_body_battery(DATE)
    live_stress = tools.get_stress(DATE)
    calls = list(fake.calls)
    assert tools.get_body_battery(DATE) == live_battery
    assert tools.get_stress(DATE) == live_stress
    assert fake.calls == calls
    for field in ("charged", "start_level", "end_level", "highest_level", "lowest_level"):
        assert field in live_battery
    assert live_stress["average_stress"] == 25


class PartlyMissingGarmin(FakeGarmin):
    def get_hrv_data(self, cdate: str) -> dict[str, Any]:
        raise GarminConnectNotFoundError("no HRV for this device")

    def get_body_battery(self, startdate: str, enddate: str | None = None) -> list[Any]:
        raise GarminConnectConnectionError("transient outage")


def test_recovery_distinguishes_missing_data_from_a_failed_read(tmp_path: Path) -> None:
    tools = GarminTools(_client(PartlyMissingGarmin()), GarminDatabase(tmp_path / "garmin.sqlite"))
    result = tools.get_recovery(DATE)
    notices = {item["field"]: item["status"] for item in result["availability"]}
    assert notices["hrv"] == "missing_or_unsupported"
    assert notices["body_battery"] == "retrieval_failed"
    assert "hrv" not in result
    assert "body_battery" not in result
    # The components Garmin did return are still present.
    assert result["sleep"]["total_sleep_seconds"] == 28000
    assert result["training_readiness"]["score"] == 75


def test_failed_recovery_component_is_not_cached_as_missing(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    tools = GarminTools(_client(PartlyMissingGarmin()), database)
    tools.get_recovery(DATE)
    assert database.fetched_at("hrv", DATE) is None
    assert database.fetched_at("body_battery", DATE) is None


def test_training_week_totals_never_treat_missing_metrics_as_zero(tmp_path: Path) -> None:
    fake = FakeGarmin()
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    tools = GarminTools(_client(fake), database)
    database.put_activity_summary(
        ActivitySummary(
            activity_id=1,
            start_time="2026-01-05 08:00:00",
            duration_seconds=600,
            distance_m=5000,
        )
    )
    # Strength work: Garmin reports no distance and no calories for it.
    database.put_activity_summary(
        ActivitySummary(activity_id=2, start_time="2026-01-06 08:00:00", duration_seconds=900)
    )
    database.mark_synced("activities", "2026-01-05:2026-01-11")

    result = tools.get_training_week("2026-01-07")
    assert result["total_duration_seconds"] == 1500
    assert result["duration_activity_count"] == 2
    assert result["total_distance_m"] == 5000
    assert result["distance_activity_count"] == 1
    # No activity reported calories, so the total is absent rather than a misleading 0.0.
    assert "total_calories_kcal" not in result
    assert result["calories_activity_count"] == 0
    coverage = {
        item["field"]: item["status"]
        for item in result["availability"]
        if item["status"] == "partial_coverage"
    }
    assert "total_distance_m" in coverage
    assert "total_calories_kcal" in coverage
    assert "total_duration_seconds" not in coverage


def test_compare_activities_discloses_how_many_activities_each_range_covers(
    tmp_path: Path,
) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    tools = GarminTools(_client(FakeGarmin()), database)
    for activity_id, power in ((1, 200.0), (2, None)):
        database.put_activity_detail(
            ActivityDetail(
                summary=ActivitySummary(
                    activity_id=activity_id,
                    start_time="2026-08-20 08:00:00",
                    duration_seconds=600,
                    average_power_w=power,
                )
            ),
            now=datetime(2026, 8, 21, 13, tzinfo=UTC),
        )
    result = tools.compare_activities([1, 2])
    deltas = {item["metric"]: item for item in result["deltas"]}
    assert deltas["average_power_w"]["values"] == {"1": 200.0, "2": None}
    assert deltas["average_power_w"]["compared_activity_count"] == 1
    assert deltas["average_power_w"]["missing_activity_count"] == 1
    assert "range" not in deltas["average_power_w"]
    assert deltas["duration_seconds"]["compared_activity_count"] == 2


def test_cycle_cache_hit_keeps_the_unsupported_notice(tmp_path: Path) -> None:
    class NoCycleGarmin(FakeGarmin):
        def get_menstrual_data_for_date(self, fordate: str) -> dict[str, Any]:
            return self._record("get_menstrual_data_for_date", {})

        def get_menstrual_calendar_data(self, startdate: str, enddate: str) -> dict[str, Any]:
            return self._record("get_menstrual_calendar_data", {})

    fake = NoCycleGarmin()
    tools = GarminTools(_client(fake), GarminDatabase(tmp_path / "garmin.sqlite"))
    first = tools.get_cycle("2026-01-01")
    second = tools.get_cycle("2026-01-01")
    assert first == second
    assert first["availability"][0]["status"] == "unsupported_or_not_configured"


def test_undated_activities_use_the_same_window_with_and_without_the_cache(
    tmp_path: Path,
) -> None:
    """The uncached path used to ask Garmin for the N most recent activities of all time."""
    fake = FakeGarmin()
    GarminTools(_client(fake)).get_activities(limit=5)
    uncached = list(fake.date_arguments)
    fake.date_arguments.clear()
    GarminTools(_client(fake), GarminDatabase(tmp_path / "garmin.sqlite")).get_activities(limit=5)
    today = date.today()
    expected = ((today - timedelta(days=13)).isoformat(), today.isoformat())
    assert uncached == [expected]
    assert fake.date_arguments == [expected]


def _seed_recovery_days(database: GarminDatabase, days: int = 7) -> None:
    for day in range(1, days + 1):
        cdate = f"2026-01-{day:02d}"
        database.put_daily(
            DailySummary(
                date=cdate,
                resting_hr_bpm=50,
                body_battery_charged=60 if day < days else 30,
                body_battery_drained=40,
            ),
            TrainingReadiness(date=cdate, score=70),
        )
        database.put_sleep(SleepSummary(date=cdate, sleep_score=80))
        database.mark_synced("readiness", cdate)


def test_trend_uses_garmins_last_night_hrv_when_nightly_average_is_absent(
    tmp_path: Path,
) -> None:
    """The installed client models hrvSummary with lastNightAvg and no nightlyAverage."""
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    tools = GarminTools(_client(FakeGarmin()), database)
    _seed_recovery_days(database)
    for day in range(1, 8):
        database.put_hrv(
            HrvSummary(
                date=f"2026-01-{day:02d}",
                last_night_average_ms=90 if day == 7 else 60,
                weekly_average_ms=62,
            )
        )

    result = tools._recovery_trend(7, date(2026, 1, 7)).compact()
    assert result["points"][-1]["hrv_nightly_average_ms"] == 90
    metrics = {item["metric"]: item for item in result["metrics"]}
    assert metrics["hrv_nightly_average_ms"]["current"] == 90
    assert metrics["hrv_nightly_average_ms"]["recent_average"] == 60
    assert metrics["hrv_nightly_average_ms"]["sample_days"] == 6
    notices = {item["field"]: item for item in result["availability"]}
    assert notices["hrv_nightly_average_ms"]["status"] == "alternate_garmin_source"


def test_trend_reports_weekly_hrv_as_a_series_without_a_rolling_mean_deviation(
    tmp_path: Path,
) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    tools = GarminTools(_client(FakeGarmin()), database)
    _seed_recovery_days(database)
    for day in range(1, 8):
        database.put_hrv(HrvSummary(date=f"2026-01-{day:02d}", weekly_average_ms=60 + day))

    result = tools._recovery_trend(7, date(2026, 1, 7)).compact()
    assert [point["hrv_weekly_average_ms"] for point in result["points"]] == [
        61,
        62,
        63,
        64,
        65,
        66,
        67,
    ]
    # Deliberately no deviation metric: consecutive weekly averages share nights.
    assert "hrv_weekly_average_ms" not in {item["metric"] for item in result["metrics"]}
    notices = {item["field"]: item for item in result["availability"]}
    assert notices["hrv_weekly_average_ms"]["status"] == "series_only_no_deviation"


def test_trend_carries_body_battery_totals_without_extra_garmin_calls(
    tmp_path: Path,
) -> None:
    fake = FakeGarmin()
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    tools = GarminTools(_client(fake), database)
    _seed_recovery_days(database)
    for day in range(1, 8):
        database.put_hrv(HrvSummary(date=f"2026-01-{day:02d}", last_night_average_ms=60))

    result = tools._recovery_trend(7, date(2026, 1, 7)).compact()
    assert [point["body_battery_charged"] for point in result["points"]][-1] == 30
    metrics = {item["metric"]: item for item in result["metrics"]}
    assert metrics["body_battery_charged"]["current"] == 30
    assert metrics["body_battery_charged"]["recent_average"] == 60
    assert metrics["body_battery_charged"]["difference"] == -30
    assert metrics["body_battery_drained"]["sample_days"] == 6
    notices = {item["field"]: item for item in result["availability"]}
    assert notices["body_battery_charged"]["status"] == "whole_day_total"
    # Existing daily values are reused; one compact sleep-range read adds the newly supported
    # overnight change, sleep HR, and skin-temperature deviation without per-day reads.
    assert fake.calls == ["get_sleep_daily"]
