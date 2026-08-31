"""Defensive, token-conscious normalization of private Garmin API responses."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from datetime import date as date_value
from typing import Any

from .models import (
    ActivityDetail,
    ActivityLap,
    ActivitySummary,
    AvailabilityNotice,
    BodyBatterySummary,
    BodyCompositionEntry,
    CycleSummary,
    DailySummary,
    HrvSummary,
    SleepSummary,
    StressSummary,
    TimePoint,
    TrainingLoad,
    TrainingReadiness,
)
from .notices import (
    DATE_MISMATCH,
    MISSING_OR_UNSUPPORTED,
    body_battery_notice,
    cycle_notices,
    derived_notice,
    unavailable_source_notice,
    unlabeled_status_notice,
)

MAX_TIMESERIES_POINTS = 48
MAX_ACTIVITY_LAPS = 200


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _first(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rounded(value: Any, digits: int) -> float | None:
    number = _number(value)
    return round(number, digits) if number is not None else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return round(number) if number is not None else None


def _text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _nested(data: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    current: Any = data
    for key in path:
        current = _map(current).get(key)
    return _map(current)


def _epoch_to_iso(value: Any) -> str | int:
    number = _number(value)
    if number is None:
        return str(value)
    # Garmin time-series timestamps are generally epoch milliseconds.
    seconds = number / 1000 if number > 10_000_000_000 else number
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return int(number)


def decimate_points(points: Sequence[Any], cap: int = MAX_TIMESERIES_POINTS) -> list[Any]:
    """Keep endpoints and evenly sampled points, never returning more than ``cap``."""
    if cap < 2:
        raise ValueError("cap must be at least 2")
    if len(points) <= cap:
        return list(points)
    last = len(points) - 1
    indexes = sorted({round(i * last / (cap - 1)) for i in range(cap)})
    return [points[index] for index in indexes]


def _points(raw: Any, cap: int | None = MAX_TIMESERIES_POINTS) -> list[TimePoint]:
    parsed: list[TimePoint] = []
    for item in _list(raw):
        if isinstance(item, list | tuple) and len(item) >= 2:
            timestamp, value = item[0], item[1]
        elif isinstance(item, Mapping):
            timestamp = _first(item, "timestamp", "startGMT", "startTimeGMT", "calendarDate")
            value = _first(item, "value", "bodyBattery", "stressLevel", "hrvValue")
        else:
            continue
        number = _number(value)
        if timestamp is not None and number is not None and number >= 0:
            parsed.append(TimePoint(timestamp=_epoch_to_iso(timestamp), value=number))
    return decimate_points(parsed, cap) if cap is not None else parsed


def normalize_daily_summary(raw: Any, date: str) -> DailySummary:
    data = _map(raw)
    return DailySummary(
        date=date,
        steps=_integer(_first(data, "totalSteps", "steps")),
        distance_m=_rounded(_first(data, "totalDistanceMeters", "distanceInMeters"), 1),
        active_calories_kcal=_number(_first(data, "activeKilocalories", "activeCalories")),
        total_calories_kcal=_number(_first(data, "totalKilocalories", "totalCalories")),
        resting_hr_bpm=_integer(_first(data, "restingHeartRate", "restingHR")),
        min_hr_bpm=_integer(_first(data, "minHeartRate", "minHr")),
        max_hr_bpm=_integer(_first(data, "maxHeartRate", "maxHr")),
        average_stress=_integer(_first(data, "averageStressLevel", "averageStress")),
        max_stress=_integer(_first(data, "maxStressLevel", "maxStress")),
        body_battery_charged=_integer(
            _first(data, "bodyBatteryChargedValue", "bodyBatteryCharged")
        ),
        body_battery_drained=_integer(
            _first(data, "bodyBatteryDrainedValue", "bodyBatteryDrained")
        ),
        # Garmin names both fields as floor counts. They are estimates and can be fractional;
        # do not mislabel the descending value as metres or coerce it to an integer.
        floors_ascended=_rounded(_first(data, "floorsAscended", "floorsClimbed"), 2),
        floors_descended=_rounded(data.get("floorsDescended"), 2),
        floors_unit=(
            "floors"
            if _first(data, "floorsAscended", "floorsClimbed", "floorsDescended") is not None
            else None
        ),
        moderate_intensity_minutes=_integer(data.get("moderateIntensityMinutes")),
        vigorous_intensity_minutes=_integer(data.get("vigorousIntensityMinutes")),
    )


def normalize_sleep(raw: Any, date: str) -> SleepSummary:
    root = _map(raw)
    data = _nested(root, "dailySleepDTO") or root
    scores = _nested(data, "sleepScores")
    overall = _nested(scores, "overall")
    score = _first(data, "sleepScore", "overallSleepScore")
    if score is None:
        score = _first(overall, "value", "score")
    return SleepSummary(
        date=date,
        sleep_score=_integer(score),
        total_sleep_seconds=_integer(_first(data, "sleepTimeSeconds", "totalSleepSeconds")),
        deep_sleep_seconds=_integer(_first(data, "deepSleepSeconds", "deepSleepDuration")),
        light_sleep_seconds=_integer(_first(data, "lightSleepSeconds", "lightSleepDuration")),
        rem_sleep_seconds=_integer(_first(data, "remSleepSeconds", "remSleepDuration")),
        awake_seconds=_integer(_first(data, "awakeSleepSeconds", "awakeTimeSeconds")),
        sleep_start=_text(
            _first(
                data, "sleepStartTimestampLocal", "sleepStartTimestampGMT", "sleepStartTimeLocal"
            )
        ),
        sleep_end=_text(
            _first(data, "sleepEndTimestampLocal", "sleepEndTimestampGMT", "sleepEndTimeLocal")
        ),
        average_respiration=_number(_first(data, "averageRespirationValue", "averageRespiration")),
        lowest_respiration=_number(_first(data, "lowestRespirationValue", "lowestRespiration")),
        highest_respiration=_number(_first(data, "highestRespirationValue", "highestRespiration")),
        average_spo2_percent=_number(_first(data, "averageSpO2Value", "averageSpo2")),
        lowest_spo2_percent=_number(_first(data, "lowestSpO2Value", "lowestSpo2")),
    )


def normalize_hrv(raw: Any, date: str, *, include_timeseries: bool = False) -> HrvSummary:
    root = _map(raw)
    summary = _nested(root, "hrvSummary") or root
    baseline = _nested(summary, "baseline") or _nested(root, "baseline")
    readings = _points(root.get("hrvReadings")) if include_timeseries else None
    return HrvSummary(
        date=date,
        status=_text(_first(summary, "status", "hrvStatus")),
        nightly_average_ms=_number(_first(summary, "nightlyAverage", "nightlyAvg")),
        weekly_average_ms=_number(_first(summary, "weeklyAvg", "weeklyAverage")),
        last_night_average_ms=_number(_first(summary, "lastNightAvg", "lastNightAverage")),
        baseline_low_ms=_number(_first(baseline, "lowUpper", "balancedLow", "low")),
        baseline_high_ms=_number(_first(baseline, "balancedUpper", "balancedHigh", "high")),
        readings=readings or None,
    )


def normalize_training_readiness(raw: Any, date: str) -> TrainingReadiness:
    entries = [_map(item) for item in _list(raw)]
    if not entries and isinstance(raw, Mapping):
        entries = [_map(raw)]
    if not entries:
        return TrainingReadiness(date=date)
    morning = next((x for x in entries if x.get("inputContext") == "AFTER_WAKEUP_RESET"), None)
    data = morning or max(
        entries, key=lambda x: str(x.get("timestampLocal") or x.get("timestamp") or "")
    )
    return TrainingReadiness(
        date=date,
        score=_integer(data.get("score")),
        level=_text(_first(data, "scoreFeedback", "level", "rating")),
        feedback=_text(_first(data, "feedbackLong", "feedbackShort", "feedback")),
        timestamp=_text(_first(data, "timestampLocal", "timestamp")),
        sleep_score=_integer(data.get("sleepScore")),
        hrv_factor_percent=_number(_first(data, "hrvFactorPercent", "hrvFactor")),
        acute_load_factor_percent=_number(
            _first(data, "acuteLoadFactorPercent", "acuteLoadFactor")
        ),
        sleep_history_factor_percent=_number(
            _first(data, "sleepHistoryFactorPercent", "sleepHistoryFactor")
        ),
        stress_history_factor_percent=_number(
            _first(data, "stressHistoryFactorPercent", "stressHistoryFactor")
        ),
        recovery_time_minutes=_integer(_first(data, "recoveryTime", "recoveryTimeMinutes")),
    )


def normalize_body_battery(
    raw: Any, date: str, *, include_timeseries: bool = False
) -> BodyBatterySummary:
    entries = [_map(item) for item in _list(raw)]
    if not entries and isinstance(raw, Mapping):
        entries = [_map(raw)]
    # Garmin's Body Battery read is range-shaped and can answer with neighbouring days.  A record
    # for another date must not be reported as this date's, so it is discarded and disclosed.
    matching = [item for item in entries if _text(item.get("date")) in (None, date)]
    if not matching:
        return BodyBatterySummary(
            date=date,
            availability=[
                body_battery_notice(
                    DATE_MISMATCH if entries else MISSING_OR_UNSUPPORTED, date
                )
            ],
        )
    data = matching[0]
    all_points = _points(
        _first(data, "bodyBatteryValuesArray", "bodyBatteryValues", "values"), cap=None
    )
    values = [float(point.value) for point in all_points]
    points = decimate_points(all_points, MAX_TIMESERIES_POINTS)
    return BodyBatterySummary(
        date=date,
        charged=_integer(data.get("charged")),
        drained=_integer(data.get("drained")),
        start_level=_integer(values[0]) if values else _integer(data.get("startLevel")),
        end_level=_integer(values[-1]) if values else _integer(data.get("endLevel")),
        highest_level=_integer(max(values)) if values else _integer(data.get("highestLevel")),
        lowest_level=_integer(min(values)) if values else _integer(data.get("lowestLevel")),
        timeseries=points if include_timeseries and points else None,
    )


def normalize_stress(raw: Any, date: str, *, include_timeseries: bool = False) -> StressSummary:
    data = _map(raw)
    points = _points(_first(data, "stressValuesArray", "stressValues", "values"))
    return StressSummary(
        date=date,
        average_stress=_integer(_first(data, "avgStressLevel", "averageStressLevel")),
        max_stress=_integer(_first(data, "maxStressLevel", "maxStress")),
        stress_duration_seconds=_integer(data.get("stressDuration")),
        rest_duration_seconds=_integer(data.get("restStressDuration")),
        low_duration_seconds=_integer(data.get("lowStressDuration")),
        medium_duration_seconds=_integer(data.get("mediumStressDuration")),
        high_duration_seconds=_integer(data.get("highStressDuration")),
        timeseries=points if include_timeseries and points else None,
    )


def _activity_type(data: Mapping[str, Any]) -> str | None:
    # Activity lists use activityType; get_activity currently uses activityTypeDTO.
    raw = _first(data, "activityType", "activityTypeDTO")
    if isinstance(raw, Mapping):
        return _text(_first(raw, "typeKey", "typeId", "parentTypeId"))
    return _text(raw)


def normalize_activity(raw: Any) -> ActivitySummary:
    data = _map(raw)
    activity_id = _integer(_first(data, "activityId", "activityID", "id"))
    if activity_id is None or activity_id <= 0:
        raise ValueError("Garmin activity response is missing a valid activity ID")
    return ActivitySummary(
        activity_id=activity_id,
        name=_text(_first(data, "activityName", "name")),
        start_time=_text(_first(data, "startTimeLocal", "startTimeGMT", "beginTimestamp")),
        activity_type=_activity_type(data),
        duration_seconds=_rounded(_first(data, "duration", "durationSeconds"), 2),
        elapsed_seconds=_rounded(_first(data, "elapsedDuration", "elapsedDurationSeconds"), 2),
        distance_m=_rounded(_first(data, "distance", "distanceMeters"), 1),
        calories_kcal=_number(_first(data, "calories", "caloriesKcal")),
        average_hr_bpm=_number(_first(data, "averageHR", "averageHeartRate")),
        max_hr_bpm=_number(_first(data, "maxHR", "maxHeartRate")),
        average_speed_mps=_rounded(
            _first(data, "averageSpeed", "averageSpeedMetersPerSecond"), 3
        ),
        elevation_gain_m=_rounded(_first(data, "elevationGain", "gainElevation"), 1),
        average_cadence=_number(
            _first(
                data,
                "averageRunningCadenceInStepsPerMinute",
                "averageBikingCadenceInRevPerMinute",
                "averageSwimCadenceInStrokesPerMinute",
                "averageCadence",
            )
        ),
        average_power_w=_number(_first(data, "avgPower", "averagePower")),
    )


def normalize_activities(raw: Any, limit: int) -> list[ActivitySummary]:
    result: list[ActivitySummary] = []
    for item in _list(raw)[:limit]:
        try:
            result.append(normalize_activity(item))
        except ValueError:
            continue
    return result


def _zones(raw: Any) -> dict[str, float]:
    result: dict[str, float] = {}
    if isinstance(raw, Mapping):
        for label, value in raw.items():
            seconds = _number(value)
            if seconds is not None:
                result[str(label)] = seconds
        if result:
            return result
        items: Iterable[Any] = raw.values()
    else:
        items = _list(raw)
    for index, item in enumerate(items, 1):
        data = _map(item)
        seconds = _number(_first(data, "secsInZone", "seconds", "duration"))
        if seconds is not None:
            label = _text(_first(data, "zoneNumber", "zone", "name")) or str(index)
            result[f"zone_{label}"] = seconds
    return result


def normalize_activity_detail(
    raw: Any, laps_raw: Any = None, hr_zones_raw: Any = None, power_zones_raw: Any = None
) -> ActivityDetail:
    data = _map(raw)
    nested_summary = _map(data.get("summaryDTO"))
    # Current Garmin responses can keep identity/type at the root while putting
    # metrics in summaryDTO. Merge both shapes instead of discarding root fields.
    summary_data = {**data, **nested_summary} if nested_summary else data
    summary = normalize_activity(summary_data)
    lap_items = _list(laps_raw) or _list(data.get("lapDTOs"))
    laps = [
        ActivityLap(
            lap_index=_integer(_first(item_map, "lapIndex", "lapNumber", "messageIndex")),
            start_time=_text(_first(item_map, "startTimeLocal", "startTimeGMT")),
            duration_seconds=_rounded(_first(item_map, "duration", "durationSeconds"), 2),
            distance_m=_rounded(_first(item_map, "distance", "distanceMeters"), 1),
            average_hr_bpm=_number(_first(item_map, "averageHR", "averageHeartRate")),
            max_hr_bpm=_number(_first(item_map, "maxHR", "maxHeartRate")),
            average_cadence=_number(_first(item_map, "averageCadence", "avgCadence")),
            average_power_w=_number(_first(item_map, "avgPower", "averagePower")),
        )
        for item_map in (_map(item) for item in lap_items[:MAX_ACTIVITY_LAPS])
    ]
    return ActivityDetail(
        summary=summary,
        training_effect_aerobic=_rounded(
            _first(summary_data, "aerobicTrainingEffect", "trainingEffect"), 1
        ),
        training_effect_anaerobic=_rounded(summary_data.get("anaerobicTrainingEffect"), 1),
        training_effect_label=_text(
            _first(summary_data, "aerobicTrainingEffectMessage", "trainingEffectLabel")
        ),
        laps=laps,
        hr_zones_seconds=(hr_zones := _zones(hr_zones_raw or data.get("heartRateDTOs"))),
        power_zones_seconds=_zones(power_zones_raw or data.get("powerDTOs")),
        hr_zones_total_seconds=(zone_total := round(sum(hr_zones.values()), 2)) or None,
        activity_duration_seconds=summary.duration_seconds,
        hr_zone_coverage_percent=(
            round(zone_total / summary.duration_seconds * 100, 1)
            if zone_total and summary.duration_seconds
            else None
        ),
    )


def _find_value(raw: Any, keys: tuple[str, ...]) -> Any:
    """Find a known scalar key in nested mappings without retaining unknown content."""
    if isinstance(raw, Mapping):
        for key in keys:
            if raw.get(key) is not None:
                return raw[key]
        for value in raw.values():
            found = _find_value(value, keys)
            if found is not None:
                return found
    elif isinstance(raw, list | tuple):
        for value in raw:
            found = _find_value(value, keys)
            if found is not None:
                return found
    return None


# Keys through which Garmin has been observed to ship the human-readable status wording.
STATUS_PHRASE_KEYS = (
    "trainingStatusFeedbackPhrase",
    "trainingStatusPhrase",
    "trainingStatusKey",
)


def _status_phrase(status_raw: Any) -> str | None:
    """Return Garmin's training-status wording, never its opaque numeric code.

    ``trainingStatus`` is a code in current responses.  Rendering it as text would make a
    meaningless number look like a Garmin status name, and garmin-owl does not ship a
    code-to-label table it cannot verify against the installed client, so a bare code yields
    no label at all -- it is reported separately as ``training_status_code``.
    """
    phrase = _find_value(status_raw, STATUS_PHRASE_KEYS)
    if isinstance(phrase, str) and not phrase.strip().isdigit():
        return _text(phrase)
    value = _find_value(status_raw, ("trainingStatus",))
    # A numeric string such as "7" is a code wearing a string's clothes, not a label.
    if isinstance(value, str) and not value.strip().isdigit():
        return _text(value)
    return None


def _status_code(status_raw: Any) -> int | None:
    """Return Garmin's numeric training-status code, whatever type it arrives as."""
    value = _find_value(status_raw, ("trainingStatus",))
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return _integer(value)
    if isinstance(value, str) and value.strip().isdigit():
        return _integer(value.strip())
    return None


def normalize_training_load(
    status_raw: Any,
    max_metrics_raw: Any,
    endurance_raw: Any,
    hill_raw: Any,
    date: str,
    unavailable_sources: Sequence[str] = (),
) -> TrainingLoad:
    """Extract only the documented/observed aggregate training fields."""
    phrase = _status_phrase(status_raw)
    code = _status_code(status_raw)
    unlabeled_code = (
        [] if phrase is not None or code is None else [unlabeled_status_notice(code)]
    )
    return TrainingLoad(
        date=date,
        training_status=phrase,
        training_status_code=code,
        # ``monthlyLoad`` is deliberately not accepted here: it is a different Garmin metric on a
        # different window, and substituting it would silently mislabel the acute load.
        acute_load=_rounded(_find_value(status_raw, ("acuteTrainingLoad", "acuteLoad")), 1),
        load_ratio=_rounded(
            _find_value(status_raw, ("acuteChronicWorkloadRatio", "loadRatio")), 2
        ),
        vo2_max=_rounded(
            _find_value(max_metrics_raw, ("vo2MaxPreciseValue", "vo2MaxValue", "vo2Max")), 1
        ),
        endurance_score=_rounded(
            _find_value(endurance_raw, ("overallScore", "enduranceScore")), 1
        ),
        hill_score=_rounded(_find_value(hill_raw, ("overallScore", "hillScore")), 1),
        availability=[
            *unlabeled_code,
            *(unavailable_source_notice(source) for source in unavailable_sources),
        ],
    )


_CYCLE_PHASES = {
    1: "menstruation",
    2: "follicular",
    3: "ovulation",
    4: "luteal",
}


def normalize_cycle(raw_day: Any, raw_calendar: Any, date: str) -> CycleSummary:
    """Normalize cycle timing only; intentionally exclude logs, notes, and symptoms."""
    day_root = _map(raw_day)
    summary = _map(day_root.get("daySummary"))
    phase_code = _integer(summary.get("currentPhase"))
    start_text = _text(summary.get("startDate"))
    fertile_start: str | None = None
    fertile_end: str | None = None
    start_offset = _integer(summary.get("fertileWindowStart"))
    window_length = _integer(summary.get("lengthOfFertileWindow"))
    if start_text and start_offset and start_offset > 0:
        try:
            start = date_value.fromisoformat(start_text) + timedelta(days=start_offset - 1)
            fertile_start = start.isoformat()
            if window_length and window_length > 0:
                fertile_end = (start + timedelta(days=window_length - 1)).isoformat()
        except ValueError:
            pass

    calendar = _map(raw_calendar)
    predicted_dates: list[str] = []
    for item in _list(calendar.get("cycleSummaries")):
        cycle = _map(item)
        predicted = cycle.get("predictedCycle") is True
        predicted_start = _text(cycle.get("startDate"))
        if predicted and predicted_start and predicted_start >= date:
            predicted_dates.append(predicted_start)

    availability: list[AvailabilityNotice] = []
    if fertile_start:
        availability.append(
            derived_notice(
                "fertile_window_start",
                "cycle_start_date + (Garmin's fertileWindowStart day-of-cycle - 1); Garmin "
                "returns day offsets, not dates.",
            )
        )
    if fertile_end:
        availability.append(
            derived_notice(
                "fertile_window_end",
                "fertile_window_start + (Garmin's lengthOfFertileWindow - 1) days.",
            )
        )
    if not summary:
        availability.append(
            cycle_notices("unsupported_or_not_configured", date)[0]
        )
    return CycleSummary(
        date=date,
        phase=_CYCLE_PHASES.get(phase_code) if phase_code is not None else None,
        phase_code=phase_code,
        day_in_cycle=_integer(summary.get("dayInCycle")),
        cycle_start_date=start_text,
        period_length_days=_integer(summary.get("periodLength")),
        cycle_type=_text(summary.get("cycleType")),
        days_until_next_phase=_integer(summary.get("daysUntilNextPhase")),
        predicted_cycle_length_days=_integer(summary.get("predictedCycleLength")),
        fertile_window_start=fertile_start,
        fertile_window_end=fertile_end,
        next_predicted_cycle_start=min(predicted_dates, default=None),
        availability=availability,
    )


def normalize_body_composition(raw: Any) -> list[BodyCompositionEntry]:
    root = _map(raw)
    items = _first(root, "dateWeightList", "dailyWeightSummaries", "weightList")
    if items is None and isinstance(raw, list):
        items = raw
    result: list[BodyCompositionEntry] = []
    for item in _list(items):
        data = _map(item)
        weight = _number(_first(data, "weight", "weightInGrams", "weightValue"))
        if weight is not None and weight > 500:
            weight /= 1000
        muscle = _number(_first(data, "muscleMass", "muscleMassInGrams"))
        if muscle is not None and muscle > 500:
            muscle /= 1000
        bone = _number(_first(data, "boneMass", "boneMassInGrams"))
        if bone is not None and bone > 100:
            bone /= 1000
        result.append(
            BodyCompositionEntry(
                timestamp=_text(_first(data, "timestampLocal", "date", "calendarDate")),
                weight_kg=weight,
                bmi=_number(data.get("bmi")),
                body_fat_percent=_number(_first(data, "bodyFat", "bodyFatPercent")),
                body_water_percent=_number(_first(data, "bodyWater", "bodyWaterPercent")),
                muscle_mass_kg=muscle,
                bone_mass_kg=bone,
                visceral_fat_rating=_number(_first(data, "visceralFat", "visceralFatRating")),
            )
        )
    return result
