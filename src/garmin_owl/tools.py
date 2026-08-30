"""Validated MCP-facing application service, independent of transport."""

from __future__ import annotations

from datetime import date, timedelta
from statistics import fmean
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .client import GarminDataClient, GarminOwlMissingDataError
from .database import GarminDatabase
from .models import (
    ActivityComparison,
    ActivityDetail,
    ActivitySummary,
    AvailabilityNotice,
    BodyBatterySummary,
    BodyCompositionEntry,
    ComparisonDelta,
    DailyRecoveryPoint,
    DailySummary,
    HrvSummary,
    RecoverySummary,
    RecoveryTrend,
    SleepSummary,
    StressSummary,
    TrainingContext,
    TrainingLoad,
    TrainingReadiness,
    TrainingWeek,
    TrendMetric,
)
from .normalize import (
    normalize_activities,
    normalize_activity_detail,
    normalize_body_battery,
    normalize_body_composition,
    normalize_cycle,
    normalize_daily_summary,
    normalize_hrv,
    normalize_sleep,
    normalize_stress,
    normalize_training_readiness,
)
from .sync import SyncEngine, dates_between

_DATE = TypeAdapter(date)
MAX_RANGE_DAYS = 366
MAX_ACTIVITIES = 100
DEFAULT_RANGE_DAYS = 30


def parse_date(value: str | None, *, default: date | None = None) -> date:
    if value is None:
        return default or date.today()
    try:
        parsed = _DATE.validate_python(value)
    except ValidationError:
        raise ValueError("date must be a real calendar date in YYYY-MM-DD format") from None
    if str(parsed) != value:
        raise ValueError("date must use exact YYYY-MM-DD format")
    return parsed


def parse_range(
    start_date: str | None,
    end_date: str | None,
    *,
    default_days: int = DEFAULT_RANGE_DAYS,
) -> tuple[str, str]:
    today = date.today()
    end = parse_date(end_date, default=today)
    start = parse_date(start_date, default=end - timedelta(days=default_days - 1))
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"date range cannot exceed {MAX_RANGE_DAYS} days")
    return start.isoformat(), end.isoformat()


def validate_activity_id(activity_id: int) -> int:
    if isinstance(activity_id, bool) or activity_id <= 0:
        raise ValueError("activity_id must be a positive integer")
    return activity_id


def _compact(value: Any) -> Any:
    if hasattr(value, "compact"):
        return value.compact()
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


def _zone_availability(hr_zones: Any, power_zones: Any) -> list[AvailabilityNotice]:
    notices = []
    for zones, field, label in (
        (hr_zones, "hr_zones_seconds", "HR-zone"),
        (power_zones, "power_zones_seconds", "power-zone"),
    ):
        if zones is None:
            notices.append(
                AvailabilityNotice(
                    field=field,
                    status="unsupported_or_unavailable",
                    message=f"Garmin did not provide activity {label} aggregates.",
                )
            )
    return notices


class GarminTools:
    def __init__(
        self,
        client: GarminDataClient | None = None,
        database: GarminDatabase | None = None,
    ) -> None:
        self.client = client or GarminDataClient()
        # Explicitly injected clients are normally unit-test fakes. Caching is opt-in there;
        # production construction always uses the local normalized database.
        self.database = (
            database
            if database is not None
            else (GarminDatabase() if client is None else None)
        )
        self.sync = SyncEngine(self.client, self.database) if self.database is not None else None

    def _ensure(self, resource: str, cdate: str) -> None:
        if self.sync is not None:
            self.sync.ensure_resource(resource, cdate)

    def get_daily_summary(self, date: str | None = None) -> dict[str, Any]:
        cdate = parse_date(date).isoformat()
        if self.database is not None:
            self._ensure("daily", cdate)
            cached = self.database.get_daily(cdate)
            if cached is not None:
                return cached.compact()
        return normalize_daily_summary(self.client.daily_summary(cdate), cdate).compact()

    def get_sleep(self, date: str | None = None) -> dict[str, Any]:
        cdate = parse_date(date).isoformat()
        if self.database is not None:
            self._ensure("sleep", cdate)
            cached = self.database.get_sleep(cdate)
            if cached is not None:
                return cached.compact()
        return normalize_sleep(self.client.sleep(cdate), cdate).compact()

    def get_hrv(self, date: str | None = None, include_timeseries: bool = False) -> dict[str, Any]:
        cdate = parse_date(date).isoformat()
        if self.database is not None and not include_timeseries:
            self._ensure("hrv", cdate)
            cached = self.database.get_hrv(cdate)
            if cached is not None:
                return cached.compact()
        return normalize_hrv(
            self.client.hrv(cdate), cdate, include_timeseries=include_timeseries
        ).compact()

    def get_training_readiness(self, date: str | None = None) -> dict[str, Any]:
        cdate = parse_date(date).isoformat()
        if self.database is not None:
            self._ensure("readiness", cdate)
            cached = self.database.get_readiness(cdate)
            if cached is not None:
                return cached.compact()
        return normalize_training_readiness(self.client.training_readiness(cdate), cdate).compact()

    def get_body_battery(
        self, date: str | None = None, include_timeseries: bool = False
    ) -> dict[str, Any]:
        cdate = parse_date(date).isoformat()
        if self.database is not None and not include_timeseries:
            self._ensure("daily", cdate)
            daily = self.database.get_daily(cdate)
            if daily is not None:
                return BodyBatterySummary(
                    date=cdate,
                    charged=daily.body_battery_charged,
                    drained=daily.body_battery_drained,
                ).compact()
        return normalize_body_battery(
            self.client.body_battery(cdate), cdate, include_timeseries=include_timeseries
        ).compact()

    def get_stress(
        self, date: str | None = None, include_timeseries: bool = False
    ) -> dict[str, Any]:
        cdate = parse_date(date).isoformat()
        if self.database is not None and not include_timeseries:
            self._ensure("daily", cdate)
            daily = self.database.get_daily(cdate)
            if daily is not None:
                return StressSummary(
                    date=cdate,
                    average_stress=daily.average_stress,
                    max_stress=daily.max_stress,
                ).compact()
        return normalize_stress(
            self.client.stress(cdate), cdate, include_timeseries=include_timeseries
        ).compact()

    def get_recovery(self, date: str | None = None) -> dict[str, Any]:
        cdate = parse_date(date).isoformat()
        if self.database is not None:
            daily_data = self.get_daily_summary(cdate)
            sleep = SleepSummary(**self.get_sleep(cdate))
            hrv = HrvSummary(**self.get_hrv(cdate))
            body = BodyBatterySummary(**self.get_body_battery(cdate))
            stress = StressSummary(**self.get_stress(cdate))
            readiness = TrainingReadiness(**self.get_training_readiness(cdate))
            return RecoverySummary(
                date=cdate,
                sleep=sleep,
                hrv=hrv,
                body_battery=body,
                stress=stress,
                resting_hr_bpm=daily_data.get("resting_hr_bpm"),
                training_readiness=readiness,
            ).compact()
        daily = normalize_daily_summary(self.client.daily_summary(cdate), cdate)
        # Each component stays separate and Garmin-sourced; no proprietary score is calculated.
        recovery = RecoverySummary(
            date=cdate,
            sleep=normalize_sleep(self.client.sleep(cdate), cdate),
            hrv=normalize_hrv(self.client.hrv(cdate), cdate),
            body_battery=normalize_body_battery(self.client.body_battery(cdate), cdate),
            stress=normalize_stress(self.client.stress(cdate), cdate),
            resting_hr_bpm=daily.resting_hr_bpm,
            training_readiness=normalize_training_readiness(
                self.client.training_readiness(cdate), cdate
            ),
        )
        return recovery.compact()

    def get_activities(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= limit <= MAX_ACTIVITIES:
            raise ValueError(f"limit must be between 1 and {MAX_ACTIVITIES}")
        if start_date is None and end_date is None:
            start, end = None, None
        else:
            start, end = parse_range(start_date, end_date)
        if self.database is not None and self.sync is not None:
            if start is None or end is None:
                end = date.today().isoformat()
                start = (date.today() - timedelta(days=13)).isoformat()
            self.sync.ensure_activities(start, end)
            return [
                item.compact()
                for item in self.database.list_activities(start, end, limit=limit)
            ]
        raw = self.client.activities(start, end, limit)
        return [_compact(item) for item in normalize_activities(raw, limit)]

    def get_activity(self, activity_id: int) -> dict[str, Any]:
        validate_activity_id(activity_id)
        if self.database is not None:
            cached = self.database.get_activity(activity_id, require_detail=True)
            if cached is not None:
                return cached.compact()
            prior = self.database.get_activity(activity_id)
            summary, hr_zones, power_zones = self.client.activity(activity_id)
            laps = summary.get("lapDTOs") if isinstance(summary, dict) else None
            detail = normalize_activity_detail(summary, laps, hr_zones, power_zones)
            detail.availability.extend(_zone_availability(hr_zones, power_zones))
            if prior is not None and detail.summary.average_cadence is None:
                detail.summary = detail.summary.model_copy(
                    update={"average_cadence": prior.summary.average_cadence}
                )
            self.database.put_activity_detail(detail)
            return detail.compact()
        summary, hr_zones, power_zones = self.client.activity(activity_id)
        laps = summary.get("lapDTOs") if isinstance(summary, dict) else None
        detail = normalize_activity_detail(summary, laps, hr_zones, power_zones)
        detail.availability.extend(_zone_availability(hr_zones, power_zones))
        return detail.compact()

    def get_body_composition(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> list[dict[str, Any]]:
        start, end = parse_range(start_date, end_date)
        if self.database is not None:
            key = f"{start}:{end}"
            if not self.database.is_range_fresh("body_composition", start, end):
                fetched_entries = normalize_body_composition(
                    self.client.body_composition(start, end)
                )
                self.database.put_body_composition(fetched_entries)
                self.database.mark_synced("body_composition", key)
            return [item.compact() for item in self.database.get_body_composition(start, end)]
        entries: list[BodyCompositionEntry] = normalize_body_composition(
            self.client.body_composition(start, end)
        )
        return [entry.compact() for entry in entries]

    def get_recent_activities(
        self,
        days: int = 14,
        activity_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if isinstance(days, bool) or not 1 <= days <= 90:
            raise ValueError("days must be between 1 and 90")
        if isinstance(limit, bool) or not 1 <= limit <= MAX_ACTIVITIES:
            raise ValueError(f"limit must be between 1 and {MAX_ACTIVITIES}")
        if activity_type is not None and not activity_type.strip():
            raise ValueError("activity_type cannot be blank")
        end = date.today()
        start = end - timedelta(days=days - 1)
        if self.database is None or self.sync is None:
            items = normalize_activities(
                self.client.activities(start.isoformat(), end.isoformat(), limit), limit
            )
            if activity_type:
                items = [
                    item
                    for item in items
                    if (item.activity_type or "").casefold() == activity_type.casefold()
                ]
            return [item.compact() for item in items[:limit]]
        self.sync.ensure_activities(start.isoformat(), end.isoformat())
        return [
            item.compact()
            for item in self.database.list_activities(
                start.isoformat(), end.isoformat(), limit=limit, activity_type=activity_type
            )
        ]

    def get_training_load(self, date: str | None = None) -> dict[str, Any]:
        cdate = parse_date(date).isoformat()
        if self.database is None or self.sync is None:
            from .normalize import normalize_training_load

            return normalize_training_load(
                self.client.training_status(cdate),
                self.client.max_metrics(cdate),
                self.client.endurance_score(cdate),
                self.client.hill_score(cdate),
                cdate,
            ).compact()
        self.sync.ensure_training_load(cdate)
        item = self.database.get_training_load(cdate)
        return item.compact() if item is not None else {"date": cdate}

    def _recovery_trend(self, days: int, end: date) -> RecoveryTrend:
        if days not in {7, 14, 28} or isinstance(days, bool):
            raise ValueError("days must be one of 7, 14, or 28")
        start = end - timedelta(days=days - 1)
        requested = dates_between(start, end)
        if self.database is None or self.sync is None:
            raise RuntimeError("recovery trends require the local normalized cache")
        for cdate in requested:
            for resource in ("daily", "sleep", "hrv", "readiness"):
                self.sync.ensure_resource(resource, cdate)
        rows = self.database.recovery_rows(start.isoformat(), end.isoformat())
        by_date = {str(row["date"]): row for row in rows}
        points = [
            DailyRecoveryPoint(
                date=cdate,
                sleep_score=by_date[cdate]["sleep_score"],
                hrv_nightly_average_ms=by_date[cdate]["nightly_avg_ms"],
                resting_hr_bpm=by_date[cdate]["resting_hr_bpm"],
                training_readiness=by_date[cdate]["training_readiness"],
                recovery_time_hours=(
                    round(by_date[cdate]["recovery_time_minutes"] / 60, 1)
                    if by_date[cdate]["recovery_time_minutes"] is not None
                    else None
                ),
            )
            for cdate in requested
            if cdate in by_date
        ]
        metrics: list[TrendMetric] = []
        for field, label in (
            ("sleep_score", "sleep_score"),
            ("hrv_nightly_average_ms", "hrv_nightly_average_ms"),
            ("resting_hr_bpm", "resting_hr_bpm"),
            ("training_readiness", "training_readiness"),
        ):
            values = [
                float(value)
                for point in points
                if (value := getattr(point, field)) is not None
            ]
            if values:
                metrics.append(
                    TrendMetric(
                        metric=label,
                        current=values[-1],
                        recent_average=round(fmean(values), 1),
                        difference=round(values[-1] - fmean(values), 1),
                        percent_difference=(
                            round((values[-1] / fmean(values) - 1) * 100, 1)
                            if fmean(values)
                            else None
                        ),
                        sample_days=len(values),
                    )
                )
        return RecoveryTrend(
            days=days,
            points=points,
            metrics=metrics,
            missing_dates=[cdate for cdate in requested if cdate not in by_date],
        )

    def get_recovery_trend(self, days: int = 7) -> dict[str, Any]:
        return self._recovery_trend(days, date.today()).compact()

    def get_training_week(self, date: str | None = None) -> dict[str, Any]:
        target = parse_date(date)
        start = target - timedelta(days=target.weekday())
        end = start + timedelta(days=6)
        items = [
            ActivitySummary(**item)
            for item in self.get_activities(start.isoformat(), end.isoformat(), 100)
        ]
        type_counts: dict[str, int] = {}
        for item in items:
            type_name = item.activity_type or "unknown"
            type_counts[type_name] = type_counts.get(type_name, 0) + 1
        zones: dict[str, float] = {}
        aerobic_max: float | None = None
        anaerobic_max: float | None = None
        if self.database is not None:
            zones, aerobic_max, anaerobic_max = self.database.weekly_activity_aggregates(
                start.isoformat(), end.isoformat()
            )
        return TrainingWeek(
            week_start=start.isoformat(),
            week_end=end.isoformat(),
            activities=items,
            activity_count=len(items),
            total_duration_seconds=round(sum(item.duration_seconds or 0 for item in items), 2),
            total_distance_m=round(sum(item.distance_m or 0 for item in items), 1),
            total_calories_kcal=round(sum(item.calories_kcal or 0 for item in items), 1),
            activity_type_counts=type_counts,
            hr_zones_seconds=zones,
            highest_aerobic_training_effect=aerobic_max,
            highest_anaerobic_training_effect=anaerobic_max,
        ).compact()

    def compare_activities(self, activity_ids: list[int]) -> dict[str, Any]:
        if not 2 <= len(activity_ids) <= 10:
            raise ValueError("activity_ids must contain between 2 and 10 IDs")
        if len(set(activity_ids)) != len(activity_ids):
            raise ValueError("activity_ids must be unique")
        details = [
            ActivityDetail(**self.get_activity(validate_activity_id(item)))
            for item in activity_ids
        ]
        deltas: list[ComparisonDelta] = []
        for field in (
            "duration_seconds", "distance_m", "average_hr_bpm", "average_speed_mps",
            "elevation_gain_m", "average_cadence", "average_power_w",
        ):
            values = {
                str(detail.summary.activity_id): getattr(detail.summary, field)
                for detail in details
            }
            present = [float(value) for value in values.values() if value is not None]
            deltas.append(
                ComparisonDelta(
                    metric=field,
                    values=values,
                    range=round(max(present) - min(present), 3) if len(present) >= 2 else None,
                )
            )
        return ActivityComparison(activities=details, deltas=deltas).compact()

    def get_training_context(self, date: str | None = None) -> dict[str, Any]:
        cdate = parse_date(date).isoformat()
        trend = self._recovery_trend(7, parse_date(date))
        comparisons = trend.metrics
        flags: list[str] = []
        for metric in comparisons:
            if metric.percent_difference is None or abs(metric.percent_difference) < 5:
                continue
            direction = "above" if metric.percent_difference > 0 else "below"
            flags.append(
                f"{metric.metric} is {abs(metric.percent_difference):.1f}% {direction} "
                f"its available {metric.sample_days}-day average"
            )
        return TrainingContext(
            date=cdate,
            daily=DailySummary(**self.get_daily_summary(cdate)),
            sleep=SleepSummary(**self.get_sleep(cdate)),
            hrv=HrvSummary(**self.get_hrv(cdate)),
            readiness=TrainingReadiness(**self.get_training_readiness(cdate)),
            training_load=TrainingLoad(**self.get_training_load(cdate)),
            recent_activities=[ActivitySummary(**item) for item in self.get_recent_activities(7)],
            comparisons=comparisons,
            flags=flags,
            availability=[
                AvailabilityNotice(
                    field="interpretation",
                    status="not_provided",
                    message="The caller should interpret these facts in context.",
                )
            ],
        ).compact()

    def get_cycle(self, date: str | None = None) -> dict[str, Any]:
        """Return normalized cycle timing without raw daily logs or free text."""
        target = parse_date(date)
        cdate = target.isoformat()
        if self.database is not None and self.database.is_fresh("cycle", cdate):
            cached = self.database.get_cycle(cdate)
            if cached is not None:
                return cached.compact()
        calendar_start = (target - timedelta(days=30)).isoformat()
        calendar_end = (target + timedelta(days=60)).isoformat()
        try:
            raw_day = self.client.cycle_day(cdate)
        except GarminOwlMissingDataError:
            raw_day = None
        try:
            raw_calendar = self.client.cycle_calendar(calendar_start, calendar_end)
        except GarminOwlMissingDataError:
            raw_calendar = None
        item = normalize_cycle(raw_day, raw_calendar, cdate)
        if self.database is not None:
            self.database.put_cycle(item)
        return item.compact()
