"""Validated MCP-facing application service, independent of transport."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, timedelta
from statistics import fmean
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .client import (
    GarminDataClient,
    GarminOwlAuthError,
    GarminOwlError,
    GarminOwlMissingDataError,
    GarminOwlRateLimitError,
    GarminOwlUnavailableError,
)
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
    normalize_training_load,
    normalize_training_readiness,
)
from .notices import (
    MISSING_OR_UNSUPPORTED,
    RATE_LIMITED,
    RETRIEVAL_FAILED,
    partial_coverage_notice,
)
from .sync import SyncEngine, dates_between, read_training_load_sources

_DATE = TypeAdapter(date)
MAX_RANGE_DAYS = 366
MAX_ACTIVITIES = 100
DEFAULT_RANGE_DAYS = 30
# Window used when get_activities is called without any date, so the cached and uncached paths
# answer the same question instead of silently differing.
DEFAULT_ACTIVITY_DAYS = 14

_FAILURE_STATUS: dict[type[GarminOwlError], str] = {
    GarminOwlMissingDataError: MISSING_OR_UNSUPPORTED,
    GarminOwlRateLimitError: RATE_LIMITED,
    GarminOwlUnavailableError: RETRIEVAL_FAILED,
}
_FAILURE_MESSAGE = {
    MISSING_OR_UNSUPPORTED: "Garmin returned no data for this metric on this date.",
    RATE_LIMITED: "Garmin rate-limited this read; the value was not retrieved, not absent.",
    RETRIEVAL_FAILED: "This Garmin read failed; the value was not retrieved, not absent.",
}


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


def _sum_present(
    values: Iterable[float | None], *, digits: int = 2
) -> tuple[float | None, int]:
    """Sum only the values Garmin actually reported, and count them.

    Returns ``None`` rather than ``0`` when nothing reported the metric, so that "no activity
    recorded distance" stays distinguishable from "the activities covered zero distance".
    """
    present = [value for value in values if value is not None]
    return (round(sum(present), digits) if present else None, len(present))


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
        """Answer from the Body Battery endpoint itself, cached under its own key.

        Serving this from the daily-summary row returned only charged/drained and silently
        dropped the start/end/highest/lowest levels the uncached path provides, so the same
        tool answered with a different shape depending on cache state.
        """
        cdate = parse_date(date).isoformat()
        if self.database is not None and not include_timeseries:
            if self.database.is_fresh("body_battery", cdate):
                cached = self.database.get_body_battery(cdate)
                if cached is not None:
                    return cached.compact()
        item = normalize_body_battery(
            self.client.body_battery(cdate), cdate, include_timeseries=include_timeseries
        )
        if self.database is not None:
            self.database.put_body_battery(item)
        return item.compact()

    def get_stress(
        self, date: str | None = None, include_timeseries: bool = False
    ) -> dict[str, Any]:
        """Answer from the stress endpoint itself, cached under its own key.

        The daily-summary row carries only the average and maximum, so serving this tool from
        it dropped every per-band duration that its description promises.
        """
        cdate = parse_date(date).isoformat()
        if self.database is not None and not include_timeseries:
            if self.database.is_fresh("stress", cdate):
                cached = self.database.get_stress(cdate)
                if cached is not None:
                    return cached.compact()
        item = normalize_stress(
            self.client.stress(cdate), cdate, include_timeseries=include_timeseries
        )
        if self.database is not None:
            self.database.put_stress(item)
        return item.compact()

    def _component[T](
        self, field: str, read: Callable[[], T], notices: list[AvailabilityNotice]
    ) -> T | None:
        """Run one component read, recording *why* it is absent instead of failing the whole.

        Authentication failure is re-raised: it makes every field unavailable, so reporting it
        as a per-metric gap would misrepresent the account state as missing device data.
        """
        try:
            return read()
        except GarminOwlAuthError:
            raise
        except GarminOwlError as exc:
            status = _FAILURE_STATUS.get(type(exc), RETRIEVAL_FAILED)
            notices.append(
                AvailabilityNotice(
                    field=field, status=status, message=_FAILURE_MESSAGE[status]
                )
            )
            return None

    def get_recovery(self, date: str | None = None) -> dict[str, Any]:
        cdate = parse_date(date).isoformat()
        notices: list[AvailabilityNotice] = []
        daily = self._component(
            "daily_summary", lambda: DailySummary(**self.get_daily_summary(cdate)), notices
        )
        # Each component stays separate and Garmin-sourced; no proprietary score is calculated.
        return RecoverySummary(
            date=cdate,
            sleep=self._component(
                "sleep", lambda: SleepSummary(**self.get_sleep(cdate)), notices
            ),
            hrv=self._component("hrv", lambda: HrvSummary(**self.get_hrv(cdate)), notices),
            body_battery=self._component(
                "body_battery",
                lambda: BodyBatterySummary(**self.get_body_battery(cdate)),
                notices,
            ),
            stress=self._component(
                "stress", lambda: StressSummary(**self.get_stress(cdate)), notices
            ),
            resting_hr_bpm=daily.resting_hr_bpm if daily is not None else None,
            training_readiness=self._component(
                "training_readiness",
                lambda: TrainingReadiness(**self.get_training_readiness(cdate)),
                notices,
            ),
            availability=notices,
        ).compact()

    def get_activities(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= limit <= MAX_ACTIVITIES:
            raise ValueError(f"limit must be between 1 and {MAX_ACTIVITIES}")
        if start_date is None and end_date is None:
            # Both paths answer the same bounded question. Previously the uncached path asked
            # Garmin for the N most recent activities of all time while the cached path silently
            # applied a 14-day window, so the same call meant two different things.
            today = date.today()
            start = (today - timedelta(days=DEFAULT_ACTIVITY_DAYS - 1)).isoformat()
            end = today.isoformat()
        else:
            start, end = parse_range(start_date, end_date)
        if self.database is not None and self.sync is not None:
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
            payloads, unavailable = read_training_load_sources(self.client, cdate)
            return normalize_training_load(*payloads, cdate, unavailable).compact()
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
        current_date = end.isoformat()
        current_point = next((point for point in points if point.date == current_date), None)
        baseline_points = [point for point in points if point.date < current_date]
        metrics: list[TrendMetric] = []
        availability: list[AvailabilityNotice] = []
        for field, label in (
            ("sleep_score", "sleep_score"),
            ("hrv_nightly_average_ms", "hrv_nightly_average_ms"),
            ("resting_hr_bpm", "resting_hr_bpm"),
            ("training_readiness", "training_readiness"),
        ):
            samples = [
                (point.date, float(value))
                for point in baseline_points
                if (value := getattr(point, field)) is not None
            ]
            values = [value for _, value in samples]
            current_value = getattr(current_point, field) if current_point is not None else None
            baseline_average = fmean(values) if values else None
            metrics.append(
                TrendMetric(
                    metric=label,
                    current_date=current_date,
                    current=float(current_value) if current_value is not None else None,
                    recent_average=(
                        round(baseline_average, 1) if baseline_average is not None else None
                    ),
                    difference=(
                        round(float(current_value) - baseline_average, 1)
                        if current_value is not None and baseline_average is not None
                        else None
                    ),
                    percent_difference=(
                        round((float(current_value) / baseline_average - 1) * 100, 1)
                        if current_value is not None and baseline_average
                        else None
                    ),
                    sample_days=len(values),
                    baseline_start=samples[0][0] if samples else None,
                    baseline_end=samples[-1][0] if samples else None,
                )
            )
            if current_value is None:
                availability.append(
                    AvailabilityNotice(
                        field=label,
                        status="missing_current",
                        message=f"Garmin did not provide {label} for {current_date}.",
                    )
                )
            if not samples:
                availability.append(
                    AvailabilityNotice(
                        field=label,
                        status="missing_baseline",
                        message=f"No preceding {label} values are available for comparison.",
                    )
                )
        return RecoveryTrend(
            days=days,
            points=points,
            metrics=metrics,
            missing_dates=[cdate for cdate in requested if cdate not in by_date],
            availability=availability,
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
        detail_count = effect_count = hr_count = 0
        if self.database is not None:
            (
                zones,
                aerobic_max,
                anaerobic_max,
                detail_count,
                effect_count,
                hr_count,
            ) = self.database.weekly_activity_aggregates(start.isoformat(), end.isoformat())
        availability: list[AvailabilityNotice] = []
        # Garmin omits distance for strength work and calories for some devices.  Summing those
        # as zero would understate the week and make an absent metric look measured, so each
        # total covers only the activities that reported it and says how many that was.
        totals = {
            "duration": _sum_present(item.duration_seconds for item in items),
            "distance": _sum_present((item.distance_m for item in items), digits=1),
            "calories": _sum_present((item.calories_kcal for item in items), digits=1),
        }
        for name, field in (
            ("duration", "total_duration_seconds"),
            ("distance", "total_distance_m"),
            ("calories", "total_calories_kcal"),
        ):
            covered = totals[name][1]
            if covered < len(items):
                availability.append(
                    partial_coverage_notice(
                        field,
                        covered,
                        len(items),
                        "Garmin reported this metric for only some activities; the total sums "
                        "those and treats the rest as missing, not zero.",
                    )
                )
        if effect_count < len(items):
            availability.append(
                partial_coverage_notice(
                    "training_effect",
                    effect_count,
                    len(items),
                    "Training-effect maxima cover only activities whose Garmin detail was "
                    "fetched and reported a training effect.",
                )
            )
        if hr_count < len(items):
            availability.append(
                partial_coverage_notice(
                    "hr_zones_seconds",
                    hr_count,
                    len(items),
                    "HR-zone totals cover only activities for which Garmin provided zone "
                    "aggregates.",
                )
            )
        return TrainingWeek(
            week_start=start.isoformat(),
            week_end=end.isoformat(),
            activities=items,
            activity_count=len(items),
            total_duration_seconds=totals["duration"][0],
            total_distance_m=totals["distance"][0],
            total_calories_kcal=totals["calories"][0],
            duration_activity_count=totals["duration"][1],
            distance_activity_count=totals["distance"][1],
            calories_activity_count=totals["calories"][1],
            activity_type_counts=type_counts,
            hr_zones_seconds=zones,
            highest_aerobic_training_effect=aerobic_max,
            highest_anaerobic_training_effect=anaerobic_max,
            detail_activity_count=detail_count,
            training_effect_activity_count=effect_count,
            hr_zones_activity_count=hr_count,
            availability=availability,
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
                    compared_activity_count=len(present),
                    missing_activity_count=len(values) - len(present),
                )
            )
        return ActivityComparison(activities=details, deltas=deltas).compact()

    def get_training_context(self, date: str | None = None) -> dict[str, Any]:
        target = parse_date(date)
        cdate = target.isoformat()
        trend = self._recovery_trend(7, target)
        activity_start = (target - timedelta(days=6)).isoformat()
        comparisons = trend.metrics
        flags: list[str] = []
        for metric in comparisons:
            if not metric.percent_difference or metric.sample_days < 3:
                continue
            direction = "above" if metric.percent_difference > 0 else "below"
            flags.append(
                f"{metric.metric} is {abs(metric.percent_difference):.1f}% {direction} "
                f"the average of {metric.sample_days} available preceding days"
            )
        notices: list[AvailabilityNotice] = []
        return TrainingContext(
            date=cdate,
            daily=self._component(
                "daily_summary", lambda: DailySummary(**self.get_daily_summary(cdate)), notices
            ),
            sleep=self._component(
                "sleep", lambda: SleepSummary(**self.get_sleep(cdate)), notices
            ),
            hrv=self._component("hrv", lambda: HrvSummary(**self.get_hrv(cdate)), notices),
            readiness=self._component(
                "training_readiness",
                lambda: TrainingReadiness(**self.get_training_readiness(cdate)),
                notices,
            ),
            training_load=self._component(
                "training_load", lambda: TrainingLoad(**self.get_training_load(cdate)), notices
            ),
            recent_activities=[
                ActivitySummary(**item)
                for item in self.get_activities(activity_start, cdate, MAX_ACTIVITIES)
            ],
            comparisons=comparisons,
            flags=flags,
            availability=[
                *trend.availability,
                *notices,
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
