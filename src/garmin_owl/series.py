"""Day-by-day history of Garmin daily metrics, read through Garmin's range endpoints.

Each metric belongs to one Garmin source. Range sources are read in the largest chunks Garmin
accepts and stored per day in ``daily_series``, including days Garmin returned nothing for, so a
long history costs a handful of requests once and nothing afterwards. The two metrics Garmin
exposes only per day (training readiness, Body Battery at wake) reuse the single-day cache and
are opt-in because a year of them is one request per day.
"""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import fmean, stdev
from typing import Any

from .client import (
    BODY_BATTERY_RANGE_DAYS,
    STRESS_RANGE_DAYS,
    GarminDataClient,
    GarminOwlAuthError,
    GarminOwlError,
    GarminOwlMissingDataError,
    GarminOwlRateLimitError,
)
from .database import GarminDatabase
from .models import (
    SKIN_TEMPERATURE_BASIS,
    AvailabilityNotice,
    DailySeries,
    DateSpan,
    MetricCoverage,
    PeriodRow,
    PeriodStat,
    PeriodSummary,
    SeriesExport,
)
from .normalize import (
    normalize_body_battery_range,
    normalize_hrv_range,
    normalize_resting_hr_range,
    normalize_sleep_range,
    normalize_stress_range,
)
from .notices import RATE_LIMITED, RETRIEVAL_FAILED
from .sync import SyncEngine

METRIC_UNITS: dict[str, str] = {
    "resting_hr": "bpm",
    "hrv_nightly": "ms",
    "hrv_weekly": "ms",
    "sleep_score": "score 0-100",
    "sleep_hr": "bpm (average during sleep)",
    "sleep_duration": "hours",
    "respiration": "breaths/min (average during sleep)",
    "skin_temp_deviation": "°C from personal baseline",
    "body_battery_charged": "points (whole day)",
    "body_battery_drained": "points (whole day)",
    "stress_avg": "score 0-100 (whole day)",
    "body_battery_at_wake": "level 0-100",
    "training_readiness": "score 0-100",
}
SERIES_METRICS = tuple(METRIC_UNITS)
# Garmin has no range read for these: one request per day, so callers must ask for them.
PER_DAY_METRICS = ("body_battery_at_wake", "training_readiness")
DEFAULT_METRICS = tuple(metric for metric in SERIES_METRICS if metric not in PER_DAY_METRICS)
_INTEGER_METRICS = frozenset(
    {
        "resting_hr",
        "sleep_score",
        "body_battery_charged",
        "body_battery_drained",
        "stress_avg",
        "body_battery_at_wake",
        "training_readiness",
    }
)
# Rolling means: consecutive values share days, so a mean or SD of them double-counts.
_ROLLING_METRICS = frozenset({"hrv_weekly"})

# Garmin requests one tool call may make before answering with what it has. Progress is cached,
# so calling again continues where this one stopped.
RANGE_REQUEST_BUDGET = 150
PER_DAY_REQUEST_BUDGET = 60
HRV_RANGE_DAYS = 366  # verified live: "Exceeded max date difference (366 days)"
SLEEP_RANGE_DAYS = 28  # Garmin's sleep stats limit; upstream would otherwise split silently
RESTING_HR_RANGE_DAYS = 366

Values = dict[str, dict[str, float | None]]


def _rhr(client: GarminDataClient, start: str, end: str) -> Values:
    return {
        cdate: {"resting_hr": value}
        for cdate, value in normalize_resting_hr_range(client.resting_hr_range(start, end)).items()
    }


def _hrv(client: GarminDataClient, start: str, end: str) -> Values:
    return {
        cdate: {
            "hrv_nightly_average": item.nightly_average_ms,
            "hrv_last_night": item.last_night_average_ms,
            "hrv_weekly": item.weekly_average_ms,
        }
        for cdate, item in normalize_hrv_range(client.hrv_range(start, end)).items()
    }


def _sleep(client: GarminDataClient, start: str, end: str) -> Values:
    return {
        cdate: {
            "sleep_score": item.sleep_score,
            "sleep_hr": item.average_hr_bpm,
            "sleep_duration": (
                round(item.total_sleep_seconds / 3600, 2)
                if item.total_sleep_seconds is not None
                else None
            ),
            "respiration": item.average_respiration,
            "skin_temp_deviation": item.skin_temperature_deviation_c,
        }
        for cdate, item in normalize_sleep_range(client.sleep_range(start, end)).items()
    }


def _body_battery(client: GarminDataClient, start: str, end: str) -> Values:
    return {
        cdate: {"body_battery_charged": item.charged, "body_battery_drained": item.drained}
        for cdate, item in normalize_body_battery_range(
            client.body_battery_range(start, end)
        ).items()
    }


def _stress(client: GarminDataClient, start: str, end: str) -> Values:
    return {
        cdate: {"stress_avg": value}
        for cdate, value in normalize_stress_range(client.stress_range(start, end)).items()
    }


@dataclass(frozen=True)
class _Source:
    name: str
    chunk_days: int
    stored: tuple[str, ...]
    read: Callable[[GarminDataClient, str, str], Values]


_SOURCES = (
    _Source("resting_hr_range", RESTING_HR_RANGE_DAYS, ("resting_hr",), _rhr),
    _Source(
        "hrv_range",
        HRV_RANGE_DAYS,
        ("hrv_nightly_average", "hrv_last_night", "hrv_weekly"),
        _hrv,
    ),
    _Source(
        "sleep_range",
        SLEEP_RANGE_DAYS,
        ("sleep_score", "sleep_hr", "sleep_duration", "respiration", "skin_temp_deviation"),
        _sleep,
    ),
    _Source(
        "body_battery_range",
        BODY_BATTERY_RANGE_DAYS,
        ("body_battery_charged", "body_battery_drained"),
        _body_battery,
    ),
    _Source("stress_range", STRESS_RANGE_DAYS, ("stress_avg",), _stress),
)
# Which stored values each public metric is built from.
_STORED_FOR = {
    **{metric: (metric,) for source in _SOURCES for metric in source.stored},
    "hrv_nightly": ("hrv_nightly_average", "hrv_last_night"),
}


def _failure_status(exc: GarminOwlError) -> str:
    return RATE_LIMITED if isinstance(exc, GarminOwlRateLimitError) else RETRIEVAL_FAILED


def _days(start: date, end: date) -> list[str]:
    count = (end - start).days + 1
    return [(start + timedelta(days=offset)).isoformat() for offset in range(count)]


def date_spans(dates: list[str]) -> list[DateSpan]:
    """Collapse sorted ISO dates into runs of consecutive days."""
    spans: list[DateSpan] = []
    for cdate in dates:
        if spans and date.fromisoformat(spans[-1].end) + timedelta(days=1) == date.fromisoformat(
            cdate
        ):
            spans[-1] = DateSpan(start=spans[-1].start, end=cdate)
        else:
            spans.append(DateSpan(start=cdate, end=cdate))
    return spans


def _span_text(dates: list[str]) -> str:
    return dates[0] if len(dates) == 1 else f"{dates[0]} to {dates[-1]}"


@dataclass
class _Budget:
    range_left: int = RANGE_REQUEST_BUDGET
    per_day_left: int = PER_DAY_REQUEST_BUDGET
    failure: GarminOwlError | None = None
    unread: dict[str, list[str]] = field(default_factory=dict)


def validate_metrics(metrics: Sequence[str] | None, *, error: type[ValueError]) -> tuple[str, ...]:
    if metrics is None:
        return DEFAULT_METRICS
    if not metrics:
        raise error("metrics must name at least one metric, or be omitted for the defaults")
    unknown = [metric for metric in metrics if metric not in METRIC_UNITS]
    if unknown:
        shown = ", ".join(repr(str(metric)[:30]) for metric in unknown[:3])
        raise error(f"unknown metric {shown}; choose from: {', '.join(SERIES_METRICS)}")
    if len(set(metrics)) != len(metrics):
        raise error("metrics must be unique")
    return tuple(metrics)


class SeriesReader:
    def __init__(
        self, client: GarminDataClient, database: GarminDatabase, sync: SyncEngine
    ) -> None:
        self.client = client
        self.database = database
        self.sync = sync

    # -- reading -------------------------------------------------------------------------

    def _fill_range(self, source: _Source, days: list[str], budget: _Budget) -> None:
        fetched = self.database.series_fetched(source.name, days[0], days[-1])
        now = datetime.now().astimezone()
        stale = [
            cdate
            for cdate in days
            if cdate not in fetched
            or not self.database.is_current(date.fromisoformat(cdate), fetched[cdate], now)
        ]
        last = date.fromisoformat(days[-1])
        while stale:
            chunk_start = date.fromisoformat(stale[0])
            chunk_end = min(chunk_start + timedelta(days=source.chunk_days - 1), last)
            chunk = _days(chunk_start, chunk_end)
            stale = [cdate for cdate in stale if cdate > chunk[-1]]
            if budget.failure is not None or budget.range_left <= 0:
                budget.unread.setdefault(source.name, []).extend(chunk)
                continue
            budget.range_left -= 1
            try:
                values = source.read(self.client, chunk[0], chunk[-1])
            except GarminOwlAuthError:
                raise
            except GarminOwlMissingDataError:
                values = {}
            except GarminOwlError as exc:
                # Stop every further read: a rate limit or outage affects them all. What was
                # read so far stays cached, so asking again later continues from here.
                budget.failure = exc
                budget.unread.setdefault(source.name, []).extend(chunk)
                continue
            self.database.put_series(
                source.name, source.stored, {cdate: values.get(cdate, {}) for cdate in chunk}
            )

    def _per_day(self, metric: str, days: list[str], budget: _Budget) -> dict[str, float]:
        resource = "readiness" if metric == "training_readiness" else "daily"
        result: dict[str, float] = {}
        for cdate in days:
            if budget.failure is None and (
                budget.per_day_left > 0 or self.database.is_fresh(resource, cdate)
            ):
                try:
                    if self.sync.ensure_resource(resource, cdate):
                        budget.per_day_left -= 1
                except GarminOwlAuthError:
                    raise
                except GarminOwlMissingDataError:
                    continue
                except GarminOwlError as exc:
                    budget.failure = exc
                    budget.unread.setdefault(metric, []).append(cdate)
                    continue
            elif not self.database.is_fresh(resource, cdate):
                budget.unread.setdefault(metric, []).append(cdate)
                continue
            if metric == "training_readiness":
                readiness = self.database.get_readiness(cdate)
                value = readiness.score if readiness is not None else None
            else:
                daily = self.database.get_daily(cdate)
                value = daily.body_battery_at_wake if daily is not None else None
            if value is not None:
                result[cdate] = float(value)
        return result

    def read(
        self, start: date, end: date, metrics: tuple[str, ...]
    ) -> tuple[list[str], dict[str, dict[str, float | int]], list[AvailabilityNotice]]:
        """Return every requested day with its values, filling the cache as needed."""
        days = _days(start, end)
        budget = _Budget()
        wanted = {stored for metric in metrics for stored in _STORED_FOR.get(metric, ())}
        for source in _SOURCES:
            if wanted & set(source.stored):
                self._fill_range(source, days, budget)
        stored = self.database.get_series(days[0], days[-1], tuple(sorted(wanted)))
        per_day = {
            metric: self._per_day(metric, days, budget)
            for metric in metrics
            if metric in PER_DAY_METRICS
        }
        values: dict[str, dict[str, float | int]] = {}
        fallback_days = 0
        for cdate in days:
            day = stored.get(cdate, {})
            row: dict[str, float | int] = {}
            for metric in metrics:
                value: float | None
                if metric in PER_DAY_METRICS:
                    value = per_day[metric].get(cdate)
                elif metric == "hrv_nightly":
                    value = day.get("hrv_nightly_average")
                    if value is None and day.get("hrv_last_night") is not None:
                        value = day["hrv_last_night"]
                        fallback_days += 1
                else:
                    value = day.get(metric)
                if value is not None:
                    row[metric] = round(value) if metric in _INTEGER_METRICS else value
            if row:
                values[cdate] = row
        return days, values, self._notices(metrics, budget, fallback_days, len(values))

    @staticmethod
    def _notices(
        metrics: tuple[str, ...], budget: _Budget, fallback_days: int, value_days: int
    ) -> list[AvailabilityNotice]:
        notices: list[AvailabilityNotice] = []
        source_metrics = {
            source.name: [
                metric
                for metric in metrics
                if set(_STORED_FOR.get(metric, ())) & set(source.stored)
            ]
            for source in _SOURCES
        }
        for key, unread in budget.unread.items():
            if not unread:
                continue
            unread = sorted(set(unread))
            affected = source_metrics.get(key, [key])
            if budget.failure is not None:
                status = _failure_status(budget.failure)
                reason = str(budget.failure)
            else:
                status = "request_budget_reached"
                reason = (
                    "This call reached its Garmin request budget "
                    f"({RANGE_REQUEST_BUDGET} range or {PER_DAY_REQUEST_BUDGET} per-day reads)."
                )
            notices.append(
                AvailabilityNotice(
                    field=",".join(affected),
                    status=status,
                    message=(
                        f"Not retrieved for {len(unread)} day(s) from {_span_text(unread)}: "
                        f"{reason} Those values are unknown, not absent. Days already read "
                        "stay cached, so calling again later continues from here."
                    ),
                )
            )
        if fallback_days:
            notices.append(
                AvailabilityNotice(
                    field="hrv_nightly",
                    status="alternate_garmin_source",
                    message=(
                        f"For {fallback_days} of {value_days} days Garmin supplied no "
                        "nightlyAverage, so Garmin's lastNightAvg was used for the same "
                        "per-night HRV average."
                    ),
                )
            )
        fixed = {
            "hrv_weekly": (
                "series_only_no_deviation",
                "Garmin's weekly HRV is already a rolling 7-day mean; period summaries give "
                "only its first and last value, never a mean or SD that would double-count "
                "nights. Use hrv_nightly for averages and variability.",
            ),
            "skin_temp_deviation": ("garmin_baseline_deviation", SKIN_TEMPERATURE_BASIS),
            "body_battery_charged": (
                "whole_day_total",
                "Garmin's whole-day Body Battery charge and drain totals, not overnight recharge.",
            ),
            "sleep_hr": (
                "different_from_resting_hr",
                "Average heart rate during sleep, a different Garmin metric from resting_hr.",
            ),
        }
        for metric, (status, message) in fixed.items():
            if metric in metrics:
                notices.append(AvailabilityNotice(field=metric, status=status, message=message))
        return notices

    # -- shaping -------------------------------------------------------------------------

    @staticmethod
    def _coverage(
        days: list[str], values: Mapping[str, Mapping[str, Any]], metrics: tuple[str, ...]
    ) -> tuple[list[MetricCoverage], list[DateSpan]]:
        coverage = []
        for metric in metrics:
            present = [cdate for cdate in days if metric in values.get(cdate, {})]
            coverage.append(
                MetricCoverage(
                    metric=metric,
                    days_with_value=len(present),
                    first_date=present[0] if present else None,
                    last_date=present[-1] if present else None,
                )
            )
        return coverage, date_spans([cdate for cdate in days if cdate not in values])

    def daily_series(
        self,
        start: date,
        end: date,
        metrics: tuple[str, ...],
        *,
        output: str = "inline",
        export_dir: Path | None = None,
    ) -> DailySeries:
        days, values, notices = self.read(start, end, metrics)
        columns = ["date", *metrics]
        rows: list[list[str | float | int | None]] = [
            [cdate, *(values[cdate].get(metric) for metric in metrics)]
            for cdate in days
            if cdate in values
        ]
        coverage, missing = self._coverage(days, values, metrics)
        export = None
        if output != "inline":
            if export_dir is None:
                raise ValueError("export_dir is required to export a series")
            export = _write_export(export_dir, output, start, end, columns, rows, metrics)
        return DailySeries(
            start_date=days[0],
            end_date=days[-1],
            metrics=list(metrics),
            units={metric: METRIC_UNITS[metric] for metric in metrics},
            columns=columns,
            rows=None if export is not None else rows,
            row_count=len(rows),
            coverage=coverage,
            missing_date_ranges=missing,
            export=export,
            availability=notices,
        )

    def period_summary(
        self, start: date, end: date, metrics: tuple[str, ...], group_by: str
    ) -> PeriodSummary:
        days, values, notices = self.read(start, end, metrics)
        groups: dict[str, list[str]] = {}
        for cdate in days:
            day = date.fromisoformat(cdate)
            if group_by == "month":
                label = f"{day.year:04d}-{day.month:02d}"
            else:
                year, week, _ = day.isocalendar()
                label = f"{year:04d}-W{week:02d}"
            groups.setdefault(label, []).append(cdate)
        periods = []
        for label, members in groups.items():
            stats: dict[str, PeriodStat] = {}
            for metric in metrics:
                series = [
                    float(values[cdate][metric])
                    for cdate in members
                    if metric in values.get(cdate, {})
                ]
                stats[metric] = _stat(series, rolling=metric in _ROLLING_METRICS)
            periods.append(
                PeriodRow(
                    period=label,
                    start_date=members[0],
                    end_date=members[-1],
                    days=len(members),
                    stats=stats,
                )
            )
        coverage, missing = self._coverage(days, values, metrics)
        return PeriodSummary(
            start_date=days[0],
            end_date=days[-1],
            group_by=group_by,
            metrics=list(metrics),
            units={metric: METRIC_UNITS[metric] for metric in metrics},
            periods=periods,
            coverage=coverage,
            missing_date_ranges=missing,
            availability=notices,
        )


def _stat(series: list[float], *, rolling: bool) -> PeriodStat:
    if not series:
        return PeriodStat(n=0)
    if rolling:
        return PeriodStat(n=len(series), first=series[0], last=series[-1])
    return PeriodStat(
        n=len(series),
        mean=round(fmean(series), 1),
        sd=round(stdev(series), 1) if len(series) >= 2 else None,
        min=min(series),
        max=max(series),
    )


def _write_export(
    directory: Path,
    output: str,
    start: date,
    end: date,
    columns: list[str],
    rows: list[list[str | float | int | None]],
    metrics: tuple[str, ...],
) -> SeriesExport:
    """Write under garmin-owl's own export folder only, with a name chosen here.

    The caller never supplies a path, so a prompt cannot make the server write elsewhere.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    path = directory / f"daily-series_{start.isoformat()}_{end.isoformat()}_{stamp}.{output}"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with open(descriptor, "w", encoding="utf-8", newline="") as handle:
        if output == "csv":
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows([["" if value is None else value for value in row] for row in rows])
        else:
            json.dump(
                {
                    "columns": columns,
                    "units": {metric: METRIC_UNITS[metric] for metric in metrics},
                    "rows": rows,
                },
                handle,
                ensure_ascii=False,
            )
    return SeriesExport(path=str(path.resolve()), format=output, row_count=len(rows))
