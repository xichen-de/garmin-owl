"""Filtering, paging and size limits of get_activities over long ranges."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_tools import FakeGarmin, _client

from garmin_owl.database import GarminDatabase
from garmin_owl.tools import ACTIVITY_LIST_CHAR_BUDGET, GarminOwlInputError, GarminTools

END = date.today() - timedelta(days=2)
START = END - timedelta(days=364)


def _activity(index: int) -> dict[str, Any]:
    """A listing shaped like Garmin's activity search, with every list-level metric filled."""
    cardio = index % 4 == 0
    start = datetime.combine(END - timedelta(days=index), datetime.min.time()) + timedelta(hours=7)
    return {
        "activityId": 10_000 + index,
        "activityName": "Cardio" if cardio else "Berlin Walking",
        "startTimeLocal": start.strftime("%Y-%m-%d %H:%M:%S"),
        "startTimeGMT": (start - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
        "activityType": {"typeKey": "indoor_cardio" if cardio else "walking"},
        "duration": 3600.0 if cardio else 600.0 + index,
        "elapsedDuration": 3700.0,
        "movingDuration": 3500.0,
        "distance": 0.0 if cardio else 900.5,
        "calories": 410.0,
        "averageHR": 132.0,
        "maxHR": 171.0,
        "averageSpeed": 1.3,
        "elevationGain": 12.0,
        "elevationLoss": 11.0,
        "averageRunningCadenceInStepsPerMinute": 101.0,
        "maxRunningCadenceInStepsPerMinute": 120.0,
        "steps": 1200,
        "avgRespirationRate": 24.1,
        "minRespirationRate": 15.0,
        "maxRespirationRate": 38.0,
        "aerobicTrainingEffect": 3.1,
        "anaerobicTrainingEffect": 1.4,
        "aerobicTrainingEffectMessage": "IMPROVING_AEROBIC_BASE_8",
        "activityTrainingLoad": 88.4,
        "vO2MaxValue": 41.0,
        "moderateIntensityMinutes": 20,
        "vigorousIntensityMinutes": 25,
        "hrTimeInZone_1": 300.0,
        "hrTimeInZone_2": 600.0,
        "hrTimeInZone_3": 900.0,
        "hrTimeInZone_4": 1200.0,
        "hrTimeInZone_5": 300.0,
        **({"description": "Class: 5 rounds, rowing and wall balls."} if cardio else {}),
    }


class YearGarmin(FakeGarmin):
    """300 activities over a year, a quarter of them hour-long classes with notes."""

    def get_activities_by_date(
        self, startdate: str, enddate: str | None = None
    ) -> list[dict[str, Any]]:
        items = [_activity(index) for index in range(300)]
        return self._record(
            "get_activities_by_date",
            [
                item
                for item in items
                if startdate <= item["startTimeLocal"][:10] <= (enddate or startdate)
            ],
        )


def _service(tmp_path: Path, cached: bool) -> GarminTools:
    database = GarminDatabase(tmp_path / "garmin.sqlite") if cached else None
    return GarminTools(_client(YearGarmin()), database)


def _text_size(payload: dict[str, Any]) -> int:
    """Size as the client receives it: compact JSON text."""
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


@pytest.mark.parametrize("cached", [False, True])
def test_type_and_duration_filters_apply_before_the_limit(tmp_path: Path, cached: bool) -> None:
    result = _service(tmp_path, cached).get_activities(
        START.isoformat(),
        END.isoformat(),
        limit=100,
        activity_types=["INDOOR_CARDIO"],
        min_duration_minutes=30,
        compact=True,
    )
    assert result["matched"] == 75 and result["count"] == 75
    assert result["truncated"] is False and "next_offset" not in result
    assert {item["activity_type"] for item in result["activities"]} == {"indoor_cardio"}
    walks = _service(tmp_path / "w", cached).get_activities(
        START.isoformat(), END.isoformat(), min_duration_minutes=60
    )
    assert walks["matched"] == 75, "10-minute walks are excluded by the duration floor"


@pytest.mark.parametrize("cached", [False, True])
def test_paging_with_next_offset_returns_every_match_once(tmp_path: Path, cached: bool) -> None:
    service = _service(tmp_path, cached)
    seen: list[int] = []
    offset: int | None = 0
    while offset is not None:
        page = service.get_activities(
            START.isoformat(), END.isoformat(), limit=40, compact=True, offset=offset, order="asc"
        )
        seen.extend(item["activity_id"] for item in page["activities"])
        offset = page.get("next_offset")
    assert len(seen) == len(set(seen)) == 300
    assert seen == sorted(seen, reverse=True), "asc is oldest first: ids fall with the date"


def test_a_full_page_stops_at_the_size_budget_with_a_cursor(tmp_path: Path) -> None:
    service = _service(tmp_path, True)
    page = service.get_activities(START.isoformat(), END.isoformat(), limit=100)
    assert page["size_capped"] is True and page["truncated"] is True
    assert page["count"] < 100 and page["next_offset"] == page["count"]
    assert _text_size(page) < ACTIVITY_LIST_CHAR_BUDGET + 2_000
    rest = service.get_activities(
        START.isoformat(), END.isoformat(), limit=100, offset=page["next_offset"]
    )
    first_ids = {item["activity_id"] for item in page["activities"]}
    assert first_ids.isdisjoint(item["activity_id"] for item in rest["activities"])


def test_compact_listings_fit_a_hundred_activities(tmp_path: Path) -> None:
    page = _service(tmp_path, True).get_activities(
        START.isoformat(), END.isoformat(), limit=100, compact=True
    )
    assert page["count"] == 100 and "size_capped" not in page
    assert _text_size(page) < ACTIVITY_LIST_CHAR_BUDGET
    keys = set().union(*(item.keys() for item in page["activities"]))
    assert "hr_zones_seconds" not in keys and "average_respiration" not in keys
    assert any(item.get("has_description") for item in page["activities"])
    assert all("description" not in item for item in page["activities"])


def test_default_responses_keep_their_previous_shape(tmp_path: Path) -> None:
    result = _service(tmp_path, True).get_activities(START.isoformat(), END.isoformat(), 5)
    assert {"offset", "order", "activity_types", "next_offset"} & set(result) == {"next_offset"}
    assert result["truncated"] is True and result["count"] == 5


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"offset": -1}, "offset"),
        ({"order": "newest"}, "order"),
        ({"min_duration_minutes": 0}, "min_duration_minutes"),
        ({"activity_types": []}, "activity_types"),
        ({"activity_types": [" "]}, "activity_types"),
    ],
)
def test_invalid_listing_options_are_rejected(
    tmp_path: Path, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(GarminOwlInputError, match=message):
        _service(tmp_path, True).get_activities(START.isoformat(), END.isoformat(), **kwargs)
