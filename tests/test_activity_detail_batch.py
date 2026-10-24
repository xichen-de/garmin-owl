"""Batch activity details and the HR-zone floors Garmin applied to each activity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from garminconnect import GarminConnectNotFoundError, GarminConnectTooManyRequestsError
from test_tools import FakeGarmin, _client

from garmin_owl.database import GarminDatabase
from garmin_owl.tools import DETAIL_BATCH_CHAR_BUDGET, GarminOwlInputError, GarminTools

NOTE = "EMOM 20: 10 wall balls, 12 cal row. Felt heavy after a bad night."


class ClassesGarmin(FakeGarmin):
    """Hour-long class sessions shaped like live detail and hrTimeInZones responses.

    Zone floors rise for later IDs, as when Garmin's max HR estimate changed.
    """

    def __init__(self, missing: frozenset[int] = frozenset()) -> None:
        super().__init__()
        self.missing = missing
        self.throttle_after: int | None = None

    def get_activity(self, activity_id: str) -> dict[str, Any]:
        if int(activity_id) in self.missing:
            raise GarminConnectNotFoundError("404")
        if self.throttle_after is not None:
            if self.throttle_after == 0:
                raise GarminConnectTooManyRequestsError("429")
            self.throttle_after -= 1
        self.activity_arguments.append(activity_id)
        return self._record(
            "get_activity",
            {
                "activityId": int(activity_id),
                "activityName": "Cardio",
                "activityTypeDTO": {"typeKey": "indoor_cardio"},
                "description": NOTE,
                "summaryDTO": {
                    "startTimeLocal": "2026-03-04T18:00:00.0",
                    "startTimeGMT": "2026-03-04T17:00:00.0",
                    "duration": 3600.0,
                    "averageHR": 138.0,
                    "maxHR": 176.0,
                    "recoveryHeartRate": 141,
                    "activityTrainingLoad": 120.5,
                    "aerobicTrainingEffect": 3.4,
                    "anaerobicTrainingEffect": 2.1,
                    "directWorkoutRpe": 70,
                    "directWorkoutFeel": 50,
                },
                "lapDTOs": [{"lapIndex": i, "duration": 300.0} for i in range(12)],
            },
        )

    def get_activity_hr_in_timezones(self, activity_id: str) -> list[dict[str, Any]]:
        self.activity_arguments.append(activity_id)
        shift = 0 if int(activity_id) < 100 else 38
        return self._record(
            "get_activity_hr_in_timezones",
            [
                {"zoneNumber": zone, "secsInZone": 600.0 + zone, "zoneLowBoundary": low + shift}
                for zone, low in zip(range(1, 6), (80, 95, 111, 127, 143), strict=True)
            ],
        )


def _tools(fake: FakeGarmin, tmp_path: Path) -> GarminTools:
    return GarminTools(_client(fake), GarminDatabase(tmp_path / "garmin.sqlite"))


def test_batch_returns_notes_ratings_recovery_hr_and_zone_floors(tmp_path: Path) -> None:
    fake = ClassesGarmin()
    tools = _tools(fake, tmp_path)
    result = tools.get_activities_detail([1, 2, 150])
    assert (result["requested"], result["returned"]) == (3, 3)
    first = result["activities"][0]
    assert first["summary"]["description"] == NOTE
    assert first["summary"]["recovery_hr_bpm"] == 141
    assert first["summary"]["perceived_effort"] == 7.0
    assert first["hr_zone_floors_bpm"] == {
        "zone_1": 80,
        "zone_2": 95,
        "zone_3": 111,
        "zone_4": 127,
        "zone_5": 143,
    }
    assert result["activities"][2]["hr_zone_floors_bpm"]["zone_5"] == 181
    assert "laps" not in first or first["laps"] == []
    # Two reads per uncached activity without power; none once cached.
    assert len(fake.calls) == 6
    assert tools.get_activities_detail([1, 2, 150]) == result
    assert len(fake.calls) == 6


def test_floors_survive_the_cache_and_a_later_list_sync(tmp_path: Path) -> None:
    fake = ClassesGarmin()
    tools = _tools(fake, tmp_path)
    live = tools.get_activity(5)
    assert live["hr_zone_floors_bpm"]["zone_1"] == 80
    tools.get_activities("2026-03-01", "2026-03-10", 10)
    assert tools.get_activity(5) == live


def test_full_detail_matches_get_activity(tmp_path: Path) -> None:
    tools = _tools(ClassesGarmin(), tmp_path)
    full = tools.get_activities_detail([9], detail="full")
    assert full["activities"] == [tools.get_activity(9)]
    assert len(full["activities"][0]["laps"]) == 12


def test_missing_ids_and_rate_limits_are_reported_per_id(tmp_path: Path) -> None:
    fake = ClassesGarmin(missing=frozenset({2}))
    fake.throttle_after = 1
    result = _tools(fake, tmp_path).get_activities_detail([1, 2, 3, 4, 5])
    assert [item["summary"]["activity_id"] for item in result["activities"]] == [1]
    statuses = {item["activity_id"]: item["status"] for item in result["not_returned"]}
    assert statuses == {
        2: "missing_or_unsupported",
        3: "retrieval_failed_rate_limited",
        4: "retrieval_failed_rate_limited",
        5: "retrieval_failed_rate_limited",
    }
    assert fake.activity_arguments.count("4") == 0, "reads stop after a rate limit"


def test_fifty_compact_details_stay_under_the_size_budget(tmp_path: Path) -> None:
    class LongNotes(ClassesGarmin):
        def get_activity(self, activity_id: str) -> dict[str, Any]:
            data = super().get_activity(activity_id)
            data["description"] = "Rounds: " + "row 500 m, 15 burpees; " * 60
            return data

    result = _tools(LongNotes(), tmp_path).get_activities_detail(list(range(1, 51)))
    size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    assert size < DETAIL_BATCH_CHAR_BUDGET
    left_out = [item for item in result["not_returned"] if item["status"] == "size_budget"]
    assert result["returned"] + len(left_out) == 50 and left_out


@pytest.mark.parametrize(
    ("ids", "message"),
    [
        ([], "between 1 and 50"),
        (list(range(1, 52)), "between 1 and 50"),
        ([3, 3], "unique"),
        ([0], "positive"),
    ],
)
def test_invalid_batches_fail_before_reading_garmin(
    tmp_path: Path, ids: list[int], message: str
) -> None:
    fake = ClassesGarmin()
    with pytest.raises(GarminOwlInputError, match=message):
        _tools(fake, tmp_path).get_activities_detail(ids)
    assert fake.calls == []


class ZoneFailureGarmin(ClassesGarmin):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def get_activity_hr_in_timezones(self, activity_id: str) -> list[dict[str, Any]]:
        self.calls.append("get_activity_hr_in_timezones")
        raise self.error


def test_a_rate_limited_zone_read_stops_the_batch_and_caches_nothing(tmp_path: Path) -> None:
    fake = ZoneFailureGarmin(GarminConnectTooManyRequestsError("429"))
    tools = _tools(fake, tmp_path)
    result = tools.get_activities_detail([1, 2, 3, 4, 5])
    assert result["returned"] == 0
    assert [item["status"] for item in result["not_returned"]] == [
        "retrieval_failed_rate_limited"
    ] * 5
    assert len(fake.calls) == 2, "one summary and one zone read, then stop"
    assert tools.database is not None
    assert tools.database.get_activity(1, require_detail=True) is None


def test_an_outage_during_a_zone_read_is_not_cached_as_absent_zones(tmp_path: Path) -> None:
    from garminconnect import GarminConnectConnectionError

    from garmin_owl.client import GarminOwlServerError

    fake = ZoneFailureGarmin(GarminConnectConnectionError("API Error 503"))
    tools = _tools(fake, tmp_path)
    with pytest.raises(GarminOwlServerError):
        tools.get_activity(1)
    assert tools.database is not None
    assert tools.database.get_activity(1, require_detail=True) is None


def test_authentication_failure_during_a_zone_read_propagates(tmp_path: Path) -> None:
    from garminconnect import GarminConnectAuthenticationError

    from garmin_owl.client import GarminOwlAuthError

    fake = ZoneFailureGarmin(GarminConnectAuthenticationError("401"))
    with pytest.raises(GarminOwlAuthError):
        _tools(fake, tmp_path).get_activities_detail([1, 2])


def test_an_activity_without_zone_data_still_returns_its_detail(tmp_path: Path) -> None:
    fake = ZoneFailureGarmin(GarminConnectNotFoundError("404"))
    result = _tools(fake, tmp_path).get_activities_detail([1, 2])
    assert result["returned"] == 2
    statuses = {item["field"]: item["status"] for item in result["activities"][0]["availability"]}
    assert statuses["hr_zones_seconds"] == "unsupported_or_unavailable"
