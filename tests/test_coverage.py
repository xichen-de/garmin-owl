"""Discovering where the user's history starts, against recorded Garmin response shapes."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from garminconnect import GarminConnectTooManyRequestsError
from test_series import HistoryGarmin
from test_tools import _client

from garmin_owl.coverage import vo2_dates
from garmin_owl.database import GarminDatabase
from garmin_owl.tools import GarminTools

TODAY = date.today()
FIRST = TODAY - timedelta(days=448)  # about 15 months, as in the motivating analysis
STRAY = FIRST - timedelta(days=200)  # an isolated early record, as on the live account


class AccountGarmin(HistoryGarmin):
    """Wellness from FIRST, HRV and sleep from a week later, VO2 max on a few days."""

    def __init__(self) -> None:
        span = (TODAY - date(2015, 1, 1)).days
        before = frozenset((FIRST - timedelta(days=i)).isoformat() for i in range(1, span)) - {
            STRAY.isoformat()
        }
        super().__init__(gaps=before)
        self.late = frozenset((FIRST + timedelta(days=i)).isoformat() for i in range(7))

    def get_hrv_data_range(self, startdate: str, enddate: str) -> dict[str, Any]:
        data = super().get_hrv_data_range(startdate, enddate)
        data["hrvSummaries"] = [
            row for row in data["hrvSummaries"] if row["calendarDate"] not in self.late
        ]
        return data

    def get_sleep_daily(self, startdate: str, enddate: str) -> list[dict[str, Any]]:
        rows = super().get_sleep_daily(startdate, enddate)
        return [row for row in rows if row["calendarDate"] not in self.late]

    def get_max_metrics_range(self, start: str, end: str) -> list[dict[str, Any]]:
        days = [FIRST + timedelta(days=3), TODAY - timedelta(days=10)]
        rows = [
            {
                "userId": 1,
                "generic": {"calendarDate": day.isoformat(), "vo2MaxPreciseValue": 41.3},
                "cycling": None,
            }
            for day in days
            if start <= day.isoformat() <= end
        ]
        return self._record("get_max_metrics_range", rows)

    def count_activities(self) -> int:
        return self._record("count_activities", 478)

    def get_activities(self, start: int = 0, limit: int = 20) -> list[dict[str, Any]]:
        # Garmin counts 478 but lists only 476, as on the live account.
        listed = [
            {
                "activityId": 476 - index,
                "startTimeLocal": f"{(TODAY - timedelta(days=index * 448 // 475)).isoformat()} "
                "08:00:00",
            }
            for index in range(476)
        ]
        return self._record("get_activities", listed[start : start + limit])

    def get_devices(self) -> list[dict[str, Any]]:
        return self._record(
            "get_devices",
            [
                {
                    "productDisplayName": "fenix 8",
                    "deviceStatus": "active",
                    "registeredDate": 1754000000000,
                    "primary": True,
                    "serialNumber": "SECRET-SERIAL",
                    "unitId": 123456789,
                    "displayName": "Owner's watch",
                }
            ],
        )


def _tools(fake: Any, tmp_path: Path) -> GarminTools:
    return GarminTools(_client(fake), GarminDatabase(tmp_path / "garmin.sqlite"))


def test_coverage_finds_each_family_with_few_requests_and_caches(tmp_path: Path) -> None:
    fake = AccountGarmin()
    tools = _tools(fake, tmp_path)
    result = tools.get_data_coverage()
    families = {item["family"]: item for item in result["families"]}
    assert families["daily_summary"]["first_date"] == STRAY.isoformat()
    assert families["daily_summary"]["regular_from"] == FIRST.isoformat()
    assert families["daily_summary"]["last_date"] == TODAY.isoformat()
    assert families["hrv"]["first_date"] == (FIRST + timedelta(days=7)).isoformat()
    assert families["sleep"]["first_date"] == (FIRST + timedelta(days=7)).isoformat()
    assert families["vo2_max"]["first_date"] == (FIRST + timedelta(days=3)).isoformat()
    assert families["activities"] == {
        "family": "activities",
        "first_date": FIRST.isoformat(),
        "last_date": TODAY.isoformat(),
        "method": families["activities"]["method"],
    }
    assert result["activity_count"] == 478
    assert "training_readiness" not in families, "no cached readiness means no claimed dates"
    assert len(fake.calls) <= 16, fake.calls
    calls = len(fake.calls)
    assert tools.get_data_coverage() == result
    assert len(fake.calls) == calls
    tools.get_data_coverage(refresh=True)
    assert len(fake.calls) > calls


def test_devices_never_expose_serials_or_nicknames(tmp_path: Path) -> None:
    result = _tools(AccountGarmin(), tmp_path).get_data_coverage()
    assert result["devices"] == [
        {
            "product_name": "fenix 8",
            "status": "active",
            "registered_date": result["devices"][0]["registered_date"],
            "primary": True,
        }
    ]
    text = str(result)
    assert "SECRET-SERIAL" not in text and "123456789" not in text and "Owner" not in text


def test_a_failed_family_is_disclosed_and_not_cached(tmp_path: Path) -> None:
    class Throttled(AccountGarmin):
        def get_max_metrics_range(self, start: str, end: str) -> list[dict[str, Any]]:
            raise GarminConnectTooManyRequestsError("429")

    fake = Throttled()
    tools = _tools(fake, tmp_path)
    result = tools.get_data_coverage()
    assert "vo2_max" not in {item["family"] for item in result["families"]}
    notice = result["availability"][0]
    assert (notice["field"], notice["status"]) == ("vo2_max", "retrieval_failed_rate_limited")
    calls = len(fake.calls)
    tools.get_data_coverage()
    assert len(fake.calls) > calls, "a partial answer must be retried, not cached"


def test_an_account_without_data_reports_no_families(tmp_path: Path) -> None:
    class Empty(HistoryGarmin):
        def __init__(self) -> None:
            super().__init__(gaps=frozenset())

        def get_rhr_daily(self, startdate: str, enddate: str) -> list[dict[str, Any]]:
            return self._record("get_rhr_daily", [])

    fake = Empty()
    result = _tools(fake, tmp_path).get_data_coverage()
    assert result["families"] == []
    assert fake.calls.count("get_rhr_daily") == 10


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([{"generic": {"calendarDate": "2026-01-02", "vo2MaxValue": 40}}], ["2026-01-02"]),
        (
            [
                {
                    "generic": None,
                    "cycling": {"calendarDate": "2026-01-03", "vo2MaxPreciseValue": 50},
                }
            ],
            ["2026-01-03"],
        ),
        ([{"generic": {"calendarDate": "2026-01-04"}}], []),
        ({"unexpected": "shape"}, []),
    ],
)
def test_vo2_dates_require_a_value(raw: Any, expected: list[str]) -> None:
    assert vo2_dates(raw) == expected


def test_regular_from_skips_isolated_early_records() -> None:
    from garmin_owl.coverage import regular_from

    days = ["2024-12-27", "2025-01-03", "2025-08-02", "2025-08-03", "2025-08-05", "2025-08-06"]
    assert regular_from(days) == "2025-08-02"
    assert regular_from(["2025-01-01", "2025-02-01"]) is None
