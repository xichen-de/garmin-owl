"""Concise output models exposed by the MCP tools."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SKIN_TEMPERATURE_BASIS = (
    "Garmin deviation from the user's calibrated personal skin-temperature baseline; "
    "not absolute or core body temperature."
)


class OwlModel(BaseModel):
    """Base model that tolerates upstream drift while emitting compact JSON."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    def compact(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
        # An empty availability list carries no information and would otherwise appear on every
        # response.  Absence of the key means "nothing to disclose", never "not checked".
        if not data.get("availability", True):
            del data["availability"]
        return data


class AvailabilityNotice(OwlModel):
    field: str
    status: str
    message: str


class TimePoint(OwlModel):
    timestamp: str | int
    value: float | int


class DailySummary(OwlModel):
    date: str
    steps: int | None = None
    distance_m: float | None = None
    active_calories_kcal: float | None = None
    total_calories_kcal: float | None = None
    resting_hr_bpm: int | None = None
    min_hr_bpm: int | None = None
    max_hr_bpm: int | None = None
    average_stress: int | None = None
    max_stress: int | None = None
    body_battery_charged: int | None = None
    body_battery_drained: int | None = None
    floors_ascended: float | None = None
    floors_descended: float | None = None
    moderate_intensity_minutes: int | None = None
    vigorous_intensity_minutes: int | None = None
    active_seconds: int | None = None
    highly_active_seconds: int | None = None
    sedentary_seconds: int | None = None
    daily_step_goal: int | None = None
    intensity_minutes_goal: int | None = None
    last_seven_days_avg_resting_hr_bpm: int | None = None
    body_battery_during_sleep: int | None = None
    body_battery_at_wake: int | None = None
    average_waking_respiration: float | None = None
    highest_respiration: float | None = None
    lowest_respiration: float | None = None
    average_spo2_percent: float | None = None
    lowest_spo2_percent: float | None = None
    # Garmin's source field is a floor estimate, not metres; fractional values are retained.
    floors_unit: str | None = None


class SleepSummary(OwlModel):
    date: str
    sleep_score: int | None = None
    total_sleep_seconds: int | None = None
    deep_sleep_seconds: int | None = None
    light_sleep_seconds: int | None = None
    rem_sleep_seconds: int | None = None
    awake_seconds: int | None = None
    sleep_start: str | None = None
    sleep_end: str | None = None
    average_respiration: float | None = None
    lowest_respiration: float | None = None
    highest_respiration: float | None = None
    average_spo2_percent: float | None = None
    lowest_spo2_percent: float | None = None
    average_hr_bpm: float | None = None
    average_stress: float | None = None
    nap_seconds: int | None = None
    awake_count: int | None = None
    restless_moments_count: int | None = None
    sleep_need_minutes: int | None = None
    sleep_need_baseline_minutes: int | None = None
    sleep_need_feedback: str | None = None
    sleep_alignment_status: str | None = None
    # Garmin reports deviation from the user's calibrated skin-temperature baseline.
    # This is not core body temperature.
    skin_temperature_deviation_c: float | None = None
    skin_temperature_calibration_days: int | None = None
    skin_temperature_basis: str | None = None
    body_battery_change: int | None = None
    sleep_score_feedback: str | None = None


class HrvSummary(OwlModel):
    date: str
    status: str | None = None
    nightly_average_ms: float | None = None
    weekly_average_ms: float | None = None
    last_night_average_ms: float | None = None
    baseline_low_ms: float | None = None
    baseline_high_ms: float | None = None
    readings: list[TimePoint] | None = None


class TrainingReadiness(OwlModel):
    date: str
    score: int | None = None
    level: str | None = None
    feedback: str | None = None
    timestamp: str | None = None
    sleep_score: int | None = None
    hrv_factor_percent: float | None = None
    acute_load_factor_percent: float | None = None
    sleep_history_factor_percent: float | None = None
    stress_history_factor_percent: float | None = None
    recovery_time_minutes: int | None = None
    hrv_factor_feedback: str | None = None
    acute_load_factor_feedback: str | None = None
    sleep_history_factor_feedback: str | None = None
    sleep_score_factor_feedback: str | None = None
    stress_history_factor_feedback: str | None = None
    recovery_time_factor_feedback: str | None = None
    recovery_time_change_phrase: str | None = None


class BodyBatterySummary(OwlModel):
    date: str
    charged: int | None = None
    drained: int | None = None
    start_level: int | None = None
    end_level: int | None = None
    highest_level: int | None = None
    lowest_level: int | None = None
    timeseries: list[TimePoint] | None = None
    availability: list[AvailabilityNotice] = Field(default_factory=list)


class StressSummary(OwlModel):
    date: str
    average_stress: int | None = None
    max_stress: int | None = None
    stress_duration_seconds: int | None = None
    rest_duration_seconds: int | None = None
    low_duration_seconds: int | None = None
    medium_duration_seconds: int | None = None
    high_duration_seconds: int | None = None
    timeseries: list[TimePoint] | None = None
    availability: list[AvailabilityNotice] = Field(default_factory=list)


class RecoverySummary(OwlModel):
    date: str
    sleep: SleepSummary | None = None
    hrv: HrvSummary | None = None
    body_battery: BodyBatterySummary | None = None
    stress: StressSummary | None = None
    resting_hr_bpm: int | None = None
    training_readiness: TrainingReadiness | None = None
    availability: list[AvailabilityNotice] = Field(default_factory=list)
    note: str = "Garmin-provided metrics only; no derived medical or recovery score."


class ActivitySummary(OwlModel):
    activity_id: int
    name: str | None = None
    start_time: str | None = None
    activity_type: str | None = None
    duration_seconds: float | None = None
    elapsed_seconds: float | None = None
    distance_m: float | None = None
    calories_kcal: float | None = None
    average_hr_bpm: float | None = None
    max_hr_bpm: float | None = None
    average_speed_mps: float | None = None
    elevation_gain_m: float | None = None
    average_cadence: float | None = None
    average_power_w: float | None = None
    moving_duration_seconds: float | None = None
    average_moving_speed_mps: float | None = None
    elevation_loss_m: float | None = None
    average_stride_length_m: float | None = None
    steps: int | None = None
    recovery_hr_bpm: int | None = None
    average_respiration: float | None = None
    lowest_respiration: float | None = None
    highest_respiration: float | None = None
    max_cadence: float | None = None
    max_power_w: float | None = None
    normalized_power_w: float | None = None
    training_stress_score: float | None = None
    intensity_factor: float | None = None
    activity_training_load: float | None = None
    vo2_max: float | None = None
    moderate_intensity_minutes: int | None = None
    vigorous_intensity_minutes: int | None = None
    aerobic_training_effect: float | None = None
    anaerobic_training_effect: float | None = None
    training_effect_label: str | None = None
    hr_zones_seconds: dict[str, float] | None = None


class ActivityLap(OwlModel):
    lap_index: int | None = None
    start_time: str | None = None
    duration_seconds: float | None = None
    distance_m: float | None = None
    average_hr_bpm: float | None = None
    max_hr_bpm: float | None = None
    average_cadence: float | None = None
    average_power_w: float | None = None


class ActivityDetail(OwlModel):
    summary: ActivitySummary
    training_effect_aerobic: float | None = None
    training_effect_anaerobic: float | None = None
    training_effect_label: str | None = None
    laps: list[ActivityLap] = Field(default_factory=list)
    hr_zones_seconds: dict[str, float] = Field(default_factory=dict)
    power_zones_seconds: dict[str, float] = Field(default_factory=dict)
    hr_zones_total_seconds: float | None = None
    activity_duration_seconds: float | None = None
    hr_zone_coverage_percent: float | None = None
    availability: list[AvailabilityNotice] = Field(default_factory=list)


class DailyRecoveryPoint(OwlModel):
    date: str
    sleep_score: int | None = None
    hrv_nightly_average_ms: float | None = None
    # Garmin's own 7-day rolling HRV mean. Carried for drift reading only; see RecoveryTrend.
    hrv_weekly_average_ms: float | None = None
    resting_hr_bpm: int | None = None
    training_readiness: int | None = None
    recovery_time_hours: float | None = None
    # Garmin's whole-day Body Battery totals from the daily summary. Not overnight recharge.
    body_battery_charged: int | None = None
    body_battery_drained: int | None = None
    body_battery_change_during_sleep: int | None = None
    average_sleep_hr_bpm: float | None = None
    skin_temperature_deviation_c: float | None = None


class TrendMetric(OwlModel):
    metric: str
    current_date: str
    current: float | None = None
    recent_average: float | None = None
    difference: float | None = None
    percent_difference: float | None = None
    sample_days: int = 0
    baseline_start: str | None = None
    baseline_end: str | None = None
    calculation: str = "Current date compared with the mean of available preceding days."


class RecoveryTrend(OwlModel):
    days: int
    points: list[DailyRecoveryPoint]
    metrics: list[TrendMetric]
    missing_dates: list[str]
    availability: list[AvailabilityNotice] = Field(default_factory=list)


class TrainingLoad(OwlModel):
    date: str
    # Garmin's own wording. Absent when Garmin returned only an unlabeled numeric code.
    training_status: str | None = None
    # The raw Garmin code, kept separate so an opaque number is never shown as a status name.
    # garmin-owl does not publish a code-to-label mapping it cannot verify.
    training_status_code: int | None = None
    acute_load: float | None = None
    load_ratio: float | None = None
    vo2_max: float | None = None
    endurance_score: float | None = None
    hill_score: float | None = None
    chronic_load: float | None = None
    acwr_percent: float | None = None
    acwr_status: str | None = None
    acwr_feedback: str | None = None
    optimal_load_min: float | None = None
    optimal_load_max: float | None = None
    weekly_load: float | None = None
    low_aerobic_load: float | None = None
    low_aerobic_target_min: float | None = None
    low_aerobic_target_max: float | None = None
    high_aerobic_load: float | None = None
    high_aerobic_target_min: float | None = None
    high_aerobic_target_max: float | None = None
    anaerobic_load: float | None = None
    anaerobic_target_min: float | None = None
    anaerobic_target_max: float | None = None
    load_focus_feedback: str | None = None
    heat_acclimation_percent: float | None = None
    altitude_acclimation_percent: float | None = None
    availability: list[AvailabilityNotice] = Field(default_factory=list)


class HeartRateZoneProfile(OwlModel):
    sport: str | None = None
    training_method: str | None = None
    max_heart_rate_bpm: int | None = None
    resting_heart_rate_bpm: int | None = None
    lactate_threshold_heart_rate_bpm: int | None = None
    zone_floors_bpm: dict[str, int] = Field(default_factory=dict)


class PowerZoneProfile(OwlModel):
    sport: str | None = None
    functional_threshold_power_w: int | None = None
    zone_floors_w: dict[str, int] = Field(default_factory=dict)


class TrainingZones(OwlModel):
    heart_rate: list[HeartRateZoneProfile] = Field(default_factory=list)
    power: list[PowerZoneProfile] = Field(default_factory=list)
    availability: list[AvailabilityNotice] = Field(default_factory=list)
    note: str = "Garmin-configured zone floors only; no missing ceilings are inferred."


class RunningTolerancePoint(OwlModel):
    date: str
    acute_distance_m: float | None = None
    acute_impact_load: float | None = None
    acute_tolerance: float | None = None
    feedback: str | None = None


class RunningTolerance(OwlModel):
    start_date: str
    end_date: str
    points: list[RunningTolerancePoint] = Field(default_factory=list)
    availability: list[AvailabilityNotice] = Field(default_factory=list)
    note: str = (
        "Garmin-provided running distance, biomechanical impact load, and tolerance only; "
        "no injury-risk prediction or recommendation."
    )


class TrainingContext(OwlModel):
    date: str
    daily: DailySummary | None = None
    sleep: SleepSummary | None = None
    hrv: HrvSummary | None = None
    readiness: TrainingReadiness | None = None
    training_load: TrainingLoad | None = None
    recent_activities: list[ActivitySummary] = Field(default_factory=list)
    comparisons: list[TrendMetric] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    availability: list[AvailabilityNotice] = Field(default_factory=list)
    note: str = "Facts and transparent comparisons only; no training or medical recommendation."


class TrainingWeek(OwlModel):
    week_start: str
    week_end: str
    activities: list[ActivitySummary]
    activity_count: int
    total_duration_seconds: float | None = None
    total_distance_m: float | None = None
    total_calories_kcal: float | None = None
    # Garmin omits distance for strength work and calories for some devices.  Each total sums
    # only the activities that actually reported the metric; these say how many that was.
    duration_activity_count: int = 0
    distance_activity_count: int = 0
    calories_activity_count: int = 0
    activity_type_counts: dict[str, int] = Field(default_factory=dict)
    hr_zones_seconds: dict[str, float] = Field(default_factory=dict)
    highest_aerobic_training_effect: float | None = None
    highest_anaerobic_training_effect: float | None = None
    detail_activity_count: int = 0
    training_effect_activity_count: int = 0
    hr_zones_activity_count: int = 0
    availability: list[AvailabilityNotice] = Field(default_factory=list)


class ComparisonDelta(OwlModel):
    metric: str
    # ``None`` marks an activity for which Garmin reported no value; it is never zero-filled.
    values: dict[str, float | None]
    range: float | None = None
    compared_activity_count: int = 0
    missing_activity_count: int = 0
    calculation: str = "Range is max - min over the activities that reported the metric."


class ActivityComparison(OwlModel):
    activities: list[ActivityDetail]
    deltas: list[ComparisonDelta]
    availability: list[AvailabilityNotice] = Field(default_factory=list)


class SyncReport(OwlModel):
    requested_dates: int
    already_fresh: int
    dates_fetched: int
    rows_inserted: int
    rows_updated: int
    # Reads for which Garmin reported no data. Nothing was stored, so these are neither
    # inserted nor updated rows, and they are retried on the next sync.
    reads_unavailable: int = 0
    api_calls: dict[str, int]


class CacheInfo(OwlModel):
    path: str
    schema_version: int
    size_bytes: int
    first_date: str | None = None
    last_date: str | None = None
    table_rows: dict[str, int]


class CycleSummary(OwlModel):
    date: str
    phase: str | None = None
    phase_code: int | None = None
    day_in_cycle: int | None = None
    cycle_start_date: str | None = None
    period_length_days: int | None = None
    cycle_type: str | None = None
    days_until_next_phase: int | None = None
    predicted_cycle_length_days: int | None = None
    fertile_window_start: str | None = None
    fertile_window_end: str | None = None
    next_predicted_cycle_start: str | None = None
    availability: list[AvailabilityNotice] = Field(default_factory=list)


class BodyCompositionEntry(OwlModel):
    timestamp: str | None = None
    weight_kg: float | None = None
    bmi: float | None = None
    body_fat_percent: float | None = None
    body_water_percent: float | None = None
    muscle_mass_kg: float | None = None
    bone_mass_kg: float | None = None
    visceral_fat_rating: float | None = None
