from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from garminconnect import GarminConnectConnectionError

from garmin_owl.client import GarminDataClient, GarminOwlUnavailableError
from garmin_owl.database import GarminDatabase
from garmin_owl.sync import SyncEngine


class SyncGarmin:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get_user_summary(self, cdate: str) -> dict[str, Any]:
        self.calls.append(("daily", cdate))
        return {"totalSteps": 1}

    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        self.calls.append(("sleep", cdate))
        return {"dailySleepDTO": {"sleepTimeSeconds": 1}}

    def get_hrv_data(self, cdate: str) -> dict[str, Any]:
        self.calls.append(("hrv", cdate))
        return {"hrvSummary": {"nightlyAverage": 1}}

    def get_training_readiness(self, cdate: str) -> list[dict[str, Any]]:
        self.calls.append(("readiness", cdate))
        return [{"score": 1}]

    def get_activities_by_date(
        self, startdate: str, enddate: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(("activities", f"{startdate}:{enddate}"))
        return []


def test_second_historical_sync_makes_zero_garmin_calls(tmp_path: Path) -> None:
    fake = SyncGarmin()
    client = GarminDataClient(fake)  # type: ignore[arg-type]
    engine = SyncEngine(client, GarminDatabase(tmp_path / "garmin.sqlite"))
    dates = ["2026-01-01", "2026-01-02"]
    first = engine.sync_dates(dates)
    assert first.api_calls == {
        "HRV": 2,
        "activities": 1,
        "daily summary": 2,
        "sleep": 2,
        "training readiness": 2,
    }
    fake.calls.clear()
    second = engine.sync_dates(dates)
    assert second.api_calls == {}
    assert fake.calls == []
    assert second.already_fresh == 2


def test_only_one_missing_resource_is_fetched(tmp_path: Path) -> None:
    fake = SyncGarmin()
    client = GarminDataClient(fake)  # type: ignore[arg-type]
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    engine = SyncEngine(client, database)
    dates = [f"2026-01-{day:02d}" for day in range(1, 29)]
    engine.sync_dates(dates, include_activities=False)
    fake.calls.clear()
    with database.connect() as connection:
        connection.execute("DELETE FROM hrv WHERE date='2026-01-14'")
    report = engine.sync_dates(dates, include_activities=False)
    assert report.api_calls == {"HRV": 1}
    assert fake.calls == [("hrv", "2026-01-14")]


class UnavailableHrvGarmin(SyncGarmin):
    def get_hrv_data(self, cdate: str) -> dict[str, Any]:
        raise GarminConnectConnectionError("temporary outage")


def test_sync_does_not_cache_transient_failure_as_missing(tmp_path: Path) -> None:
    client = GarminDataClient(UnavailableHrvGarmin())  # type: ignore[arg-type]
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    engine = SyncEngine(client, database)

    with pytest.raises(GarminOwlUnavailableError):
        engine.sync_dates(["2026-01-01"], include_activities=False)

    assert database.fetched_at("hrv", "2026-01-01") is None
