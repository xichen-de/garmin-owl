"""VO2 max history from Garmin's max-metrics range endpoint (live response shape)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from garminconnect import GarminConnectTooManyRequestsError
from test_tools import FakeGarmin, _client

from garmin_owl.database import GarminDatabase
from garmin_owl.normalize import normalize_vo2max_range
from garmin_owl.tools import GarminTools

END = date.today() - timedelta(days=3)


class MaxMetricsGarmin(FakeGarmin):
    """A new estimate every third day; cycling on some days, as Garmin's payload allows."""

    def __init__(self) -> None:
        super().__init__()
        self.throttle = False

    def get_max_metrics_range(self, start: str, end: str) -> list[dict[str, Any]]:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        assert (last - first).days < 366
        if self.throttle:
            raise GarminConnectTooManyRequestsError("429")
        rows = []
        day = first
        while day <= last:
            if day.toordinal() % 3 == 0:
                rows.append(
                    {
                        "userId": 1,
                        "generic": {
                            "calendarDate": day.isoformat(),
                            "vo2MaxPreciseValue": 40.0 + (day.toordinal() % 10) / 10,
                            "vo2MaxValue": 40,
                            "fitnessAge": 33.0,
                            "maxMetCategory": 0,
                        },
                        "cycling": (
                            {"calendarDate": day.isoformat(), "vo2MaxPreciseValue": 44.2}
                            if day.toordinal() % 9 == 0
                            else None
                        ),
                        "heatAltitudeAcclimation": {},
                    }
                )
            day += timedelta(days=1)
        return self._record("get_max_metrics_range", rows)


def _tools(fake: FakeGarmin, tmp_path: Path) -> GarminTools:
    return GarminTools(_client(fake), GarminDatabase(tmp_path / "garmin.sqlite"))


def test_fifteen_months_of_vo2max_cost_two_requests_once(tmp_path: Path) -> None:
    fake = MaxMetricsGarmin()
    tools = _tools(fake, tmp_path)
    start = (END - timedelta(days=449)).isoformat()
    result = tools.get_vo2max_history(start, END.isoformat())
    assert fake.calls == ["get_max_metrics_range"] * 2
    generic = [point for point in result["points"] if point["sport"] == "generic"]
    cycling = [point for point in result["points"] if point["sport"] == "cycling"]
    assert len(generic) == 150 and len(cycling) == 50
    assert all(point["fitness_age"] == 33.0 for point in generic)
    assert all("fitness_age" not in point for point in cycling)
    assert result["count"] == 200 and "availability" not in result
    assert tools.get_vo2max_history(start, END.isoformat()) == result
    assert fake.calls == ["get_max_metrics_range"] * 2


def test_a_range_without_estimates_says_so(tmp_path: Path) -> None:
    result = _tools(FakeGarmin(), tmp_path).get_vo2max_history(
        (END - timedelta(days=30)).isoformat(), END.isoformat()
    )
    assert result["count"] == 0
    assert result["availability"][0]["status"] == "missing_or_unsupported"


def test_a_rate_limit_is_unknown_not_absent(tmp_path: Path) -> None:
    fake = MaxMetricsGarmin()
    fake.throttle = True
    result = _tools(fake, tmp_path).get_vo2max_history(
        (END - timedelta(days=30)).isoformat(), END.isoformat()
    )
    statuses = [item["status"] for item in result["availability"]]
    assert statuses[0] == "retrieval_failed_rate_limited"
    assert "unknown, not absent" in result["availability"][0]["message"]


def test_normalizer_prefers_precise_values_and_drops_empty_entries() -> None:
    raw = [
        {"generic": {"calendarDate": "2026-01-02", "vo2MaxPreciseValue": 41.34, "vo2MaxValue": 41}},
        {"generic": {"calendarDate": "2026-01-03", "vo2MaxValue": 0}, "cycling": None},
        {"generic": None, "cycling": {"calendarDate": "2026-01-04", "vo2MaxValue": 45}},
    ]
    assert normalize_vo2max_range(raw) == {
        "2026-01-02": {"vo2max_generic": 41.3, "fitness_age": None},
        "2026-01-04": {"vo2max_cycling": 45.0},
    }


def test_a_history_without_fitness_age_says_so(tmp_path: Path) -> None:
    class NoFitnessAge(MaxMetricsGarmin):
        def get_max_metrics_range(self, start: str, end: str) -> list[dict[str, Any]]:
            rows = super().get_max_metrics_range(start, end)
            for row in rows:
                row["generic"]["fitnessAge"] = None  # as on the live account
            return rows

    result = _tools(NoFitnessAge(), tmp_path).get_vo2max_history(
        (END - timedelta(days=30)).isoformat(), END.isoformat()
    )
    assert result["count"] > 0
    assert all("fitness_age" not in point for point in result["points"])
    notice = next(item for item in result["availability"] if item["field"] == "fitness_age")
    assert notice["status"] == "missing_or_unsupported"
