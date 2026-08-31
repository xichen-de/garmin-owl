import json

import pytest

from garmin_owl.normalize import (
    MAX_TIMESERIES_POINTS,
    decimate_points,
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

DATE = "2026-08-30"


def test_daily_summary_normalizes_known_fields() -> None:
    result = normalize_daily_summary(
        {
            "totalSteps": 8765,
            "totalDistanceMeters": 6543.2,
            "totalKilocalories": 2345,
            "restingHeartRate": 49,
            "averageStressLevel": 22,
            "floorsAscended": 7,
            "moderateIntensityMinutes": 31,
        },
        DATE,
    ).compact()
    assert result["steps"] == 8765
    assert result["distance_m"] == 6543.2
    assert result["resting_hr_bpm"] == 49
    assert "max_hr_bpm" not in result


def test_fractional_floors_are_labeled_and_not_rounded_to_integer() -> None:
    result = normalize_daily_summary(
        {"floorsAscended": 7, "floorsDescended": 6.12762}, DATE
    ).compact()
    assert result["floors_ascended"] == 7
    assert result["floors_descended"] == 6.13
    assert result["floors_unit"] == "floors"


def test_sleep_handles_nested_partial_response() -> None:
    result = normalize_sleep(
        {
            "dailySleepDTO": {
                "sleepTimeSeconds": 27000,
                "deepSleepSeconds": 4200,
                "sleepScores": {"overall": {"value": 81}},
                "averageSpO2Value": 96.4,
            }
        },
        DATE,
    ).compact()
    assert result == {
        "date": DATE,
        "sleep_score": 81,
        "total_sleep_seconds": 27000,
        "deep_sleep_seconds": 4200,
        "average_spo2_percent": 96.4,
    }


def test_unexpected_shapes_degrade_to_date_only() -> None:
    assert normalize_daily_summary(["changed"], DATE).compact() == {"date": DATE}
    assert normalize_sleep("changed", DATE).compact() == {"date": DATE}
    assert normalize_stress(None, DATE).compact() == {"date": DATE}


def test_timeseries_is_decimated_and_not_returned_by_default() -> None:
    raw = {
        "date": DATE,
        "charged": 50,
        "bodyBatteryValuesArray": [[1_700_000_000_000 + i * 60_000, i % 101] for i in range(1000)],
    }
    default = normalize_body_battery([raw], DATE).compact()
    included = normalize_body_battery([raw], DATE, include_timeseries=True).compact()
    assert "timeseries" not in default
    assert len(included["timeseries"]) <= MAX_TIMESERIES_POINTS
    assert included["timeseries"][0]["value"] == 0
    assert included["timeseries"][-1]["value"] == 90


def test_hrv_decimation_and_baseline() -> None:
    raw = {
        "hrvSummary": {
            "status": "BALANCED",
            "nightlyAverage": 54,
            "baseline": {"balancedLow": 45, "balancedHigh": 65},
        },
        "hrvReadings": [{"timestamp": i, "hrvValue": 50 + i % 5} for i in range(200)],
    }
    result = normalize_hrv(raw, DATE, include_timeseries=True).compact()
    assert result["status"] == "BALANCED"
    assert result["baseline_low_ms"] == 45
    assert len(result["readings"]) <= MAX_TIMESERIES_POINTS


def test_training_readiness_prefers_morning_snapshot() -> None:
    result = normalize_training_readiness(
        [
            {"score": 72, "timestampLocal": "2026-08-30T15:00:00"},
            {
                "score": 66,
                "inputContext": "AFTER_WAKEUP_RESET",
                "timestampLocal": "2026-08-30T07:00:00",
                "recoveryTime": 180,
            },
        ],
        DATE,
    ).compact()
    assert result["score"] == 66
    assert result["recovery_time_minutes"] == 180


def test_activity_detail_excludes_streams_and_location() -> None:
    raw = {
        "activityId": 123,
        "activityName": "Morning run",
        "startTimeLocal": "2026-08-30 07:00:00",
        "activityType": {"typeKey": "running"},
        "duration": 1800,
        "distance": 5000,
        "averageHR": 140,
        "aerobicTrainingEffect": 3.1,
        "latitude": 51.0,
        "longitude": 13.0,
        "geoPolylineDTO": {"polyline": "large-sensitive-location-stream"},
        "lapDTOs": [{"lapIndex": 1, "duration": 900, "distance": 2500}],
    }
    result = normalize_activity_detail(
        raw, raw["lapDTOs"], [{"zoneNumber": 2, "secsInZone": 600}]
    ).compact()
    serialized = json.dumps(result)
    assert result["summary"]["activity_id"] == 123
    assert result["laps"][0]["distance_m"] == 2500
    assert result["hr_zones_seconds"] == {"zone_2": 600.0}
    assert "latitude" not in serialized
    assert "polyline" not in serialized


def test_activity_detail_accepts_numeric_zone_mapping() -> None:
    result = normalize_activity_detail(
        {"activityId": 123}, hr_zones_raw={"zone1": 60, "zone2": 120}
    ).compact()
    assert result["hr_zones_seconds"] == {"zone1": 60.0, "zone2": 120.0}


def test_activity_zone_coverage_cadence_and_raw_enum_are_preserved() -> None:
    result = normalize_activity_detail(
        {
            "activityId": 123,
            "duration": 1000,
            "averageBikingCadenceInRevPerMinute": 82,
            "aerobicTrainingEffectMessage": "RECOVERY_5",
        },
        hr_zones_raw={"zone_2": 250, "zone_3": 500},
    ).compact()
    assert result["summary"]["average_cadence"] == 82
    assert result["training_effect_label"] == "RECOVERY_5"
    assert result["hr_zones_total_seconds"] == 750
    assert result["activity_duration_seconds"] == 1000
    assert result["hr_zone_coverage_percent"] == 75


def test_walking_activity_merges_root_identity_with_summary_dto() -> None:
    raw = {
        "activityId": 24_169_622_553,
        "activityTypeDTO": {"typeKey": "walking"},
        "summaryDTO": {
            "activityName": "Walk",
            "duration": 1800,
            "distance": 2200,
        },
    }
    result = normalize_activity_detail(raw).compact()
    assert result["summary"] == {
        "activity_id": 24_169_622_553,
        "name": "Walk",
        "activity_type": "walking",
        "duration_seconds": 1800.0,
        "distance_m": 2200.0,
    }
    assert result["laps"] == []
    assert result["hr_zones_seconds"] == {}
    assert result["power_zones_seconds"] == {}
    assert "training_effect_aerobic" not in result
    assert "training_effect_anaerobic" not in result


def test_invalid_activity_entries_are_skipped() -> None:
    result = normalize_activities([{"name": "missing id"}, {"activityId": 4}], 20)
    assert [item.activity_id for item in result] == [4]


def test_body_composition_converts_grams() -> None:
    result = normalize_body_composition(
        {
            "dateWeightList": [
                {
                    "calendarDate": DATE,
                    "weight": 70250,
                    "muscleMass": 33000,
                    "boneMass": 3100,
                    "bodyFat": 18.2,
                }
            ]
        }
    )[0].compact()
    assert result["weight_kg"] == 70.25
    assert result["muscle_mass_kg"] == 33
    assert result["bone_mass_kg"] == 3.1


def test_decimation_validates_cap() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        decimate_points([1, 2], 1)


def test_cycle_normalization_excludes_sensitive_day_log() -> None:
    result = normalize_cycle(
        {
            "daySummary": {
                "currentPhase": 4,
                "dayInCycle": 20,
                "startDate": "2026-08-11",
                "periodLength": 5,
                "cycleType": "REGULAR",
                "fertileWindowStart": 10,
                "lengthOfFertileWindow": 6,
            },
            "dayLog": {
                "notes": "private note",
                "symptoms": ["private symptom"],
                "sexualActivity": "private value",
            },
        },
        {
            "cycleSummaries": [
                {"predictedCycle": True, "startDate": "2026-09-08"}
            ]
        },
        DATE,
    ).compact()
    assert result["phase"] == "luteal"
    assert result["day_in_cycle"] == 20
    assert result["fertile_window_start"] == "2026-08-20"
    assert result["fertile_window_end"] == "2026-08-25"
    assert result["next_predicted_cycle_start"] == "2026-09-08"
    serialized = json.dumps(result)
    assert "private" not in serialized
    assert "symptoms" not in serialized
    assert "sexual" not in serialized


def test_body_battery_never_reports_another_dates_record() -> None:
    """Garmin's range-shaped read can answer with a neighbouring day."""
    raw = [{"date": "2026-08-29", "charged": 40, "drained": 10}]
    result = normalize_body_battery(raw, "2026-08-30").compact()
    assert result == {
        "date": "2026-08-30",
        "availability": [
            {
                "field": "body_battery",
                "status": "date_mismatch",
                "message": result["availability"][0]["message"],
            }
        ],
    }
    assert "charged" not in result
    matched = normalize_body_battery(
        [{"date": "2026-08-30", "charged": 40}], "2026-08-30"
    ).compact()
    assert matched["charged"] == 40
    assert "availability" not in matched


def test_missing_body_battery_is_missing_not_unmatched() -> None:
    result = normalize_body_battery([], "2026-08-30").compact()
    assert result["availability"][0]["status"] == "missing_or_unsupported"


def test_training_load_keeps_acute_load_and_monthly_load_distinct() -> None:
    """``monthlyLoad`` is a different Garmin metric on a different window."""
    result = normalize_training_load(
        {"monthlyLoad": 900}, None, None, None, DATE
    ).compact()
    assert "acute_load" not in result
    assert normalize_training_load({"acuteTrainingLoad": 300}, None, None, None, DATE).compact()[
        "acute_load"
    ] == 300


def test_training_status_code_is_not_rendered_as_a_status_name() -> None:
    numeric = normalize_training_load({"trainingStatus": 3}, None, None, None, DATE).compact()
    assert "training_status" not in numeric
    phrased = normalize_training_load(
        {"trainingStatus": 3, "trainingStatusFeedbackPhrase": "PRODUCTIVE_1"},
        None,
        None,
        None,
        DATE,
    ).compact()
    assert phrased["training_status"] == "PRODUCTIVE_1"


def test_training_load_discloses_unavailable_sources() -> None:
    result = normalize_training_load(
        {"acuteTrainingLoad": 300}, None, None, None, DATE, ["max_metrics", "hill_score"]
    ).compact()
    assert [item["field"] for item in result["availability"]] == ["max_metrics", "hill_score"]
    assert all(item["status"] == "missing_or_unsupported" for item in result["availability"])
    assert "vo2_max" not in result


def test_derived_fertile_window_is_labelled_as_a_garmin_owl_calculation() -> None:
    raw_day = {
        "daySummary": {
            "currentPhase": 2,
            "startDate": "2026-08-01",
            "fertileWindowStart": 11,
            "lengthOfFertileWindow": 6,
        }
    }
    result = normalize_cycle(raw_day, None, "2026-08-12").compact()
    assert result["fertile_window_start"] == "2026-08-11"
    assert result["fertile_window_end"] == "2026-08-16"
    derived = {
        item["field"]: item
        for item in result["availability"]
        if item["status"] == "garmin_owl_derived"
    }
    assert set(derived) == {"fertile_window_start", "fertile_window_end"}
    assert "cycle_start_date" in derived["fertile_window_start"]["message"]


def test_numeric_training_status_is_kept_as_a_code_not_a_label() -> None:
    """Garmin ships an unlabeled code; garmin-owl reports it without inventing a meaning."""
    for raw in ({"trainingStatus": 7}, {"trainingStatus": "7"}):
        result = normalize_training_load(raw, None, None, None, DATE).compact()
        assert result["training_status_code"] == 7
        assert "training_status" not in result
        notice = next(
            item for item in result["availability"] if item["field"] == "training_status"
        )
        assert notice["status"] == "code_without_label"
        assert "7" in notice["message"]


def test_garmin_supplied_wording_wins_and_clears_the_code_warning() -> None:
    result = normalize_training_load(
        {
            "mostRecentTrainingStatus": {
                "latestTrainingStatusData": {
                    "3442": {"trainingStatus": 7, "trainingStatusFeedbackPhrase": "UNPRODUCTIVE_1"}
                }
            }
        },
        None,
        None,
        None,
        DATE,
    ).compact()
    assert result["training_status"] == "UNPRODUCTIVE_1"
    assert result["training_status_code"] == 7
    assert "availability" not in result
