"""Long-range daily series and period summaries, against recorded Garmin response shapes.

The fake mirrors the live payloads of each range endpoint (verified 2026-10-24) and enforces
the range limits Garmin enforces, so a chunking error fails here instead of against Garmin.
"""

from __future__ import annotations

import csv
import json
import stat
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from garminconnect import GarminConnectTooManyRequestsError
from test_tools import FakeGarmin, _client

from garmin_owl.database import GarminDatabase
from garmin_owl.normalize import normalize_stress_range
from garmin_owl.series import DEFAULT_METRICS, SERIES_METRICS
from garmin_owl.tools import GarminOwlInputError, GarminTools

END = date.today() - timedelta(days=3)
SIZE_BUDGET = 48_000
LIMITS = {
    "get_rhr_daily": 366,
    "get_hrv_data_range": 366,
    "get_sleep_daily": 28,
    "get_body_battery": 31,
    "stress": 28,
}


def _span(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


class HistoryGarmin(FakeGarmin):
    """Every day has data except ``gaps``; HRV uses lastNightAvg as current accounts do."""

    def __init__(self, gaps: frozenset[str] = frozenset()) -> None:
        super().__init__()
        self.gaps = gaps
        self.fail_after: dict[str, int] = {}

    def _days(self, name: str, start: str, end: str) -> list[str]:
        days = _span(start, end)
        assert 1 <= len(days) <= LIMITS[name], (name, start, end)
        remaining = self.fail_after.get(name)
        if remaining is not None:
            if remaining == 0:
                raise GarminConnectTooManyRequestsError("429")
            self.fail_after[name] = remaining - 1
        return [day for day in days if day not in self.gaps]

    @staticmethod
    def _n(day: str) -> int:
        return date.fromisoformat(day).toordinal()

    def get_rhr_daily(self, startdate: str, enddate: str) -> list[dict[str, Any]]:
        rows = [
            {"calendarDate": day, "value": 50 + self._n(day) % 5}
            for day in self._days("get_rhr_daily", startdate, enddate)
        ]
        return self._record("get_rhr_daily", rows)

    def get_hrv_data_range(self, startdate: str, enddate: str) -> dict[str, Any]:
        rows = [
            {
                "calendarDate": day,
                "lastNightAvg": 40 + self._n(day) % 7,
                "weeklyAvg": 43,
                "status": "BALANCED",
            }
            for day in self._days("get_hrv_data_range", startdate, enddate)
        ]
        return self._record("get_hrv_data_range", {"hrvSummaries": rows, "userProfilePk": 1})

    def get_sleep_daily(self, startdate: str, enddate: str) -> list[dict[str, Any]]:
        rows = [
            {
                "calendarDate": day,
                "values": {
                    "sleepScore": 70 + self._n(day) % 20,
                    "avgHeartRate": 55.25,
                    "totalSleepTimeInSeconds": 27000,
                    "respiration": 14.5,
                    "skinTempC": -0.3,
                },
            }
            for day in self._days("get_sleep_daily", startdate, enddate)
        ]
        return self._record("get_sleep_daily", rows)

    def get_body_battery(self, startdate: str, enddate: str | None = None) -> list[dict[str, Any]]:
        rows = [
            {"date": day, "charged": 60, "drained": 55}
            for day in self._days("get_body_battery", startdate, enddate or startdate)
        ]
        return self._record("get_body_battery", rows)

    def connectapi(self, path: str, **kwargs: Any) -> Any:
        prefix = "/usersummary-service/stats/stress/daily/"
        assert path.startswith(prefix), path
        start, end = path.removeprefix(prefix).split("/")
        rows = [
            {"calendarDate": day, "values": {"overallStressLevel": 30, "highStressDuration": 60}}
            for day in self._days("stress", start, end)
        ]
        return self._record("stress_daily", rows)


def _tools(fake: FakeGarmin, tmp_path: Path) -> GarminTools:
    return GarminTools(_client(fake), GarminDatabase(tmp_path / "garmin.sqlite"))


def _start(days: int) -> str:
    return (END - timedelta(days=days - 1)).isoformat()


def _size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def test_a_year_of_daily_metrics_costs_a_few_range_reads_once(tmp_path: Path) -> None:
    fake = HistoryGarmin()
    tools = _tools(fake, tmp_path)
    first = tools.get_daily_series(_start(366), END.isoformat())
    assert first["row_count"] == 366
    assert first["columns"] == ["date", *DEFAULT_METRICS]
    counts = {name: fake.calls.count(name) for name in set(fake.calls)}
    assert counts == {
        "get_rhr_daily": 1,
        "get_hrv_data_range": 1,
        "get_sleep_daily": 14,
        "get_body_battery": 12,
        "stress_daily": 14,
    }
    calls = len(fake.calls)
    assert tools.get_daily_series(_start(366), END.isoformat()) == first
    assert len(fake.calls) == calls, "a cached year must not read Garmin again"


def test_missing_days_are_omitted_never_zero(tmp_path: Path) -> None:
    gap = (END - timedelta(days=5)).isoformat()
    fake = HistoryGarmin(gaps=frozenset({gap}))
    result = _tools(fake, tmp_path).get_daily_series(
        _start(10), END.isoformat(), ["resting_hr", "sleep_score"]
    )
    assert [row[0] for row in result["rows"]] == [
        d for d in _span(_start(10), END.isoformat()) if d != gap
    ]
    assert result["missing_date_ranges"] == [{"start": gap, "end": gap}]
    assert all(value not in (0, None) for row in result["rows"] for value in row[1:])
    assert {item["metric"]: item["days_with_value"] for item in result["coverage"]} == {
        "resting_hr": 9,
        "sleep_score": 9,
    }


def test_a_partially_missing_day_keeps_null_for_the_absent_metric(tmp_path: Path) -> None:
    class NoStressGarmin(HistoryGarmin):
        def connectapi(self, path: str, **kwargs: Any) -> Any:
            return self._record("stress_daily", [])

    result = _tools(NoStressGarmin(), tmp_path).get_daily_series(
        _start(3), END.isoformat(), ["resting_hr", "stress_avg"]
    )
    assert all(row[2] is None for row in result["rows"])
    coverage = {item["metric"]: item for item in result["coverage"]}
    assert coverage["stress_avg"] == {"metric": "stress_avg", "days_with_value": 0}


def test_hrv_fallback_and_metric_qualifiers_are_disclosed(tmp_path: Path) -> None:
    result = _tools(HistoryGarmin(), tmp_path).get_daily_series(
        _start(7), END.isoformat(), ["hrv_nightly", "hrv_weekly", "skin_temp_deviation"]
    )
    statuses = {item["field"]: item["status"] for item in result["availability"]}
    assert statuses["hrv_nightly"] == "alternate_garmin_source"
    assert statuses["hrv_weekly"] == "series_only_no_deviation"
    assert statuses["skin_temp_deviation"] == "garmin_baseline_deviation"
    assert result["units"]["skin_temp_deviation"].startswith("°C")


def test_rate_limit_keeps_what_was_read_and_the_next_call_continues(tmp_path: Path) -> None:
    fake = HistoryGarmin()
    fake.fail_after = {"get_sleep_daily": 2}
    tools = _tools(fake, tmp_path)
    first = tools.get_daily_series(_start(120), END.isoformat(), ["resting_hr", "sleep_score"])
    notice = next(item for item in first["availability"] if item["field"] == "sleep_score")
    assert notice["status"] == "retrieval_failed_rate_limited"
    assert "unknown, not absent" in notice["message"]
    coverage = {item["metric"]: item["days_with_value"] for item in first["coverage"]}
    assert coverage == {"resting_hr": 120, "sleep_score": 56}
    fake.fail_after = {}
    fake.calls.clear()
    second = tools.get_daily_series(_start(120), END.isoformat(), ["resting_hr", "sleep_score"])
    assert fake.calls == ["get_sleep_daily"] * 3
    assert not any(item["field"] == "sleep_score" for item in second.get("availability", []))


def test_per_day_metrics_are_opt_in_and_budgeted(tmp_path: Path) -> None:
    fake = HistoryGarmin()
    tools = _tools(fake, tmp_path)
    tools.get_daily_series(_start(90), END.isoformat())
    assert "get_training_readiness" not in fake.calls
    result = tools.get_daily_series(_start(90), END.isoformat(), ["training_readiness"])
    assert fake.calls.count("get_training_readiness") == 60
    notice = result["availability"][0]
    assert notice["status"] == "request_budget_reached"
    assert notice["message"].startswith("Not retrieved for 30 day(s)")
    again = tools.get_daily_series(_start(90), END.isoformat(), ["training_readiness"])
    assert fake.calls.count("get_training_readiness") == 90
    assert "availability" not in again
    assert again["coverage"][0]["days_with_value"] == 90


def test_monthly_summary_statistics_exclude_missing_days(tmp_path: Path) -> None:
    start, end = date(END.year, END.month, 1) - timedelta(days=62), END
    gaps = frozenset(_span((end - timedelta(days=9)).isoformat(), end.isoformat()))
    fake = HistoryGarmin(gaps=gaps)
    result = _tools(fake, tmp_path).get_period_summary(
        start.isoformat(), end.isoformat(), "month", ["resting_hr", "hrv_weekly"]
    )
    periods = result["periods"]
    assert periods[0]["start_date"] == start.isoformat()
    assert sum(period["days"] for period in periods) == (end - start).days + 1
    first = periods[0]["stats"]["resting_hr"]
    values = [
        50 + date.fromisoformat(d).toordinal() % 5
        for d in _span(periods[0]["start_date"], periods[0]["end_date"])
    ]
    assert first["n"] == len(values)
    assert first["mean"] == round(sum(values) / len(values), 1)
    assert (first["min"], first["max"]) == (min(values), max(values))
    assert set(periods[0]["stats"]["hrv_weekly"]) == {"n", "first", "last"}
    last = periods[-1]["stats"]["resting_hr"]
    assert last["n"] == periods[-1]["days"] - len(
        gaps & set(_span(periods[-1]["start_date"], periods[-1]["end_date"]))
    )


def test_weekly_summary_uses_iso_weeks(tmp_path: Path) -> None:
    result = _tools(HistoryGarmin(), tmp_path).get_period_summary(
        _start(28), END.isoformat(), "week", ["resting_hr"]
    )
    for period in result["periods"]:
        start = date.fromisoformat(period["start_date"])
        year, week, _ = start.isocalendar()
        assert period["period"] == f"{year:04d}-W{week:02d}"
        assert period["days"] <= 7


def test_output_stays_under_the_size_budget(tmp_path: Path) -> None:
    fake = HistoryGarmin()
    tools = _tools(fake, tmp_path)
    year = tools.get_daily_series(_start(366), END.isoformat(), list(DEFAULT_METRICS))
    assert _size(year) < SIZE_BUDGET, _size(year)
    long = tools.get_period_summary(_start(1096), END.isoformat(), "month")
    assert len(long["periods"]) >= 36
    assert _size(long) < SIZE_BUDGET, _size(long)
    weeks = tools.get_period_summary(_start(366), END.isoformat(), "week")
    assert _size(weeks) < SIZE_BUDGET, _size(weeks)


def test_export_writes_only_into_the_private_exports_folder(tmp_path: Path) -> None:
    tools = _tools(HistoryGarmin(), tmp_path)
    result = tools.get_daily_series(_start(5), END.isoformat(), ["resting_hr"], output="csv")
    path = Path(result["export"]["path"])
    assert path.parent == (tmp_path / "exports").resolve()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "rows" not in result and result["row_count"] == 5
    with path.open(newline="") as handle:
        table = list(csv.reader(handle))
    assert table[0] == ["date", "resting_hr"] and len(table) == 6
    exported = tools.get_daily_series(_start(5), END.isoformat(), ["resting_hr"], output="json")
    assert json.loads(Path(exported["export"]["path"]).read_text())["columns"] == table[0]


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda t: t.get_daily_series(metrics=["vo2"]), "unknown metric"),
        (lambda t: t.get_daily_series(metrics=[]), "at least one"),
        (lambda t: t.get_daily_series(metrics=["resting_hr", "resting_hr"]), "unique"),
        (lambda t: t.get_daily_series(output="xlsx"), "output must be"),
        (lambda t: t.get_daily_series(_start(367), END.isoformat()), "366"),
        (
            lambda t: t.get_daily_series(end_date=(date.today() + timedelta(days=1)).isoformat()),
            "future",
        ),
        (lambda t: t.get_period_summary(_start(1097), END.isoformat()), "1096"),
        (lambda t: t.get_period_summary(_start(367), END.isoformat(), "week"), "366"),
        (lambda t: t.get_period_summary(group_by="year"), "group_by"),
    ],
)
def test_invalid_requests_fail_before_reading_garmin(
    tmp_path: Path, call: Any, message: str
) -> None:
    fake = HistoryGarmin()
    with pytest.raises(GarminOwlInputError, match=message):
        call(_tools(fake, tmp_path))
    assert fake.calls == []


def test_every_metric_is_documented_with_a_unit(tmp_path: Path) -> None:
    result = _tools(HistoryGarmin(), tmp_path).get_daily_series(
        _start(2), END.isoformat(), list(SERIES_METRICS)
    )
    assert set(result["units"]) == set(SERIES_METRICS)


def test_stress_range_drops_garmins_negative_not_enough_data_levels() -> None:
    raw = [
        {"calendarDate": "2026-10-01", "values": {"overallStressLevel": 31}},
        {"calendarDate": "2026-10-02", "values": {"overallStressLevel": -1}},
        {"calendarDate": "2026-10-03", "values": {}},
    ]
    assert normalize_stress_range(raw) == {"2026-10-01": 31}
