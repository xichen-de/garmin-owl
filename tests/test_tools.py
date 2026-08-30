from __future__ import annotations

import json
from typing import Any

import pytest
from garminconnect import GarminConnectNotFoundError

from garmin_owl.client import GarminDataClient
from garmin_owl.database import GarminDatabase
from garmin_owl.tools import GarminTools, parse_date, parse_range, validate_activity_id

DATE = "2026-08-30"


class FakeGarmin:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.activity_arguments: list[str] = []

    def _record(self, name: str, result: Any) -> Any:
        self.calls.append(name)
        return result

    def get_user_summary(self, cdate: str) -> dict[str, Any]:
        return self._record("get_user_summary", {"totalSteps": 1000, "restingHeartRate": 50})

    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        return self._record("get_sleep_data", {"dailySleepDTO": {"sleepTimeSeconds": 28000}})

    def get_hrv_data(self, cdate: str) -> dict[str, Any]:
        return self._record("get_hrv_data", {"hrvSummary": {"nightlyAverage": 55}})

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


@pytest.fixture
def fake() -> FakeGarmin:
    return FakeGarmin()


@pytest.fixture
def tools(fake: FakeGarmin) -> GarminTools:
    return GarminTools(GarminDataClient(fake))


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
    assert set(fake.calls) == {
        "get_user_summary",
        "get_sleep_data",
        "get_hrv_data",
        "get_training_readiness",
        "get_body_battery",
        "get_stress_data",
        "get_activities",
        "get_activity",
        "get_activity_hr_in_timezones",
        "get_activity_power_in_timezones",
        "get_weigh_ins",
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
    tools = GarminTools(GarminDataClient(fake))
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


def test_activity_detail_is_served_from_cache_without_new_calls(fake: FakeGarmin, tmp_path) -> None:
    tools = GarminTools(
        GarminDataClient(fake), GarminDatabase(tmp_path / "garmin.sqlite")
    )
    first = tools.get_activity(123)
    first_calls = list(fake.calls)
    second = tools.get_activity(123)
    assert first == second
    assert fake.calls == first_calls


def test_v2_bound_validation(tools: GarminTools) -> None:
    with pytest.raises(ValueError, match="1 and 90"):
        tools.get_recent_activities(days=91)
    with pytest.raises(ValueError, match="one of 7, 14, or 28"):
        tools.get_recovery_trend(8)
    with pytest.raises(ValueError, match="between 2 and 10"):
        tools.compare_activities([1])
    with pytest.raises(ValueError, match="unique"):
        tools.compare_activities([1, 1])


class CycleGarmin(FakeGarmin):
    def get_menstrual_data_for_date(self, fordate: str) -> dict[str, Any]:
        return self._record(
            "get_menstrual_data_for_date",
            {"daySummary": {"currentPhase": 1, "dayInCycle": 2}},
        )

    def get_menstrual_calendar_data(
        self, startdate: str, enddate: str
    ) -> dict[str, Any]:
        return self._record("get_menstrual_calendar_data", {"cycleSummaries": []})


def test_cycle_is_on_demand_normalized_and_cached(tmp_path) -> None:
    fake = CycleGarmin()
    tools = GarminTools(
        GarminDataClient(fake), GarminDatabase(tmp_path / "garmin.sqlite")
    )
    first = tools.get_cycle("2026-01-01")
    first_calls = list(fake.calls)
    second = tools.get_cycle("2026-01-01")
    assert first == second
    assert first["phase"] == "menstruation"
    assert fake.calls == first_calls == [
        "get_menstrual_data_for_date",
        "get_menstrual_calendar_data",
    ]
