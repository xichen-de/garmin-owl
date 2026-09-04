from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from garmin_owl.database import SCHEMA_VERSION, GarminDatabase
from garmin_owl.models import (
    ActivityDetail,
    ActivitySummary,
    DailySummary,
    HrvSummary,
    SleepSummary,
    TrainingReadiness,
)
from garmin_owl.normalize import normalize_training_load


def test_schema_version_permissions_and_no_sensitive_columns(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "private" / "garmin.sqlite")
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        sql = " ".join(
            str(row[0])
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ).lower()
    for forbidden in (
        "cookie",
        "token",
        "authorization",
        "email",
        "latitude",
        "longitude",
        "polyline",
        "raw_json",
    ):
        assert forbidden not in sql
    assert database.path.stat().st_mode & 0o077 == 0
    assert database.path.parent.stat().st_mode & 0o077 == 0


def test_upsert_has_no_duplicates_and_clear_preserves_database(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    first = DailySummary(date="2026-01-01", steps=100)
    second = DailySummary(date="2026-01-01", steps=200)
    assert database.put_daily(first)
    assert not database.put_daily(second)
    assert database.get_daily("2026-01-01").steps == 200  # type: ignore[union-attr]
    assert database.info().table_rows["daily_metrics"] == 1
    database.clear()
    assert database.path.exists()
    assert database.info().table_rows["daily_metrics"] == 0


def test_freshness_today_and_recently_captured_rows(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    local = datetime(2026, 8, 30, 10, tzinfo=UTC)
    database.put_daily(DailySummary(date="2026-08-30"), now=local - timedelta(minutes=19))
    database.put_daily(DailySummary(date="2026-08-29"), now=local)
    database.put_daily(DailySummary(date="2026-08-28"), now=local)
    assert database.is_fresh("daily", "2026-08-30", now=local)
    assert not database.is_fresh("daily", "2026-08-30", now=local + timedelta(minutes=2))
    # Yesterday's row was captured this morning, before the day settles at noon: reusable
    # briefly, but re-fetched once it settles so a late device upload is picked up.
    assert database.is_fresh("daily", "2026-08-29", now=local)
    assert not database.is_fresh("daily", "2026-08-29", now=local + timedelta(minutes=21))
    assert not database.is_fresh("daily", "2026-08-29", now=local.replace(hour=13))
    # 2026-08-28 settled at 2026-08-29 12:00, before this row was captured.
    assert database.is_fresh("daily", "2026-08-28", now=local)


def test_row_captured_before_its_day_settled_is_refetched(tmp_path: Path) -> None:
    """A partially synchronized day must not stay authoritative forever."""
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    # Captured at 09:00 on the day itself, when the watch had not finished uploading.
    captured = datetime(2026, 8, 20, 9, tzinfo=UTC)
    database.put_daily(DailySummary(date="2026-08-20", steps=1), now=captured)
    assert not database.is_fresh("daily", "2026-08-20", now=datetime(2026, 8, 25, 9, tzinfo=UTC))

    # Re-captured after the day settled: now trustworthy indefinitely.
    settled = datetime(2026, 8, 21, 12, tzinfo=UTC)
    database.put_daily(DailySummary(date="2026-08-20", steps=9), now=settled)
    assert database.is_fresh("daily", "2026-08-20", now=datetime(2026, 8, 25, 9, tzinfo=UTC))


def test_activity_range_captured_mid_window_is_refetched(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    database.mark_synced(
        "activities", "2026-08-17:2026-08-20", now=datetime(2026, 8, 20, 9, tzinfo=UTC)
    )
    later = datetime(2026, 8, 24, 9, tzinfo=UTC)
    assert not database.is_activity_range_fresh("2026-08-17", "2026-08-20", now=later)
    database.mark_synced(
        "activities", "2026-08-17:2026-08-20", now=datetime(2026, 8, 21, 13, tzinfo=UTC)
    )
    assert database.is_activity_range_fresh("2026-08-17", "2026-08-20", now=later)


def test_activity_detail_captured_before_settlement_is_refetched(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    detail = ActivityDetail(
        summary=ActivitySummary(activity_id=7, start_time="2026-08-20 08:00:00")
    )
    database.put_activity_detail(detail, now=datetime(2026, 8, 20, 9, tzinfo=UTC))
    later = datetime(2026, 8, 25, 9, tzinfo=UTC)
    assert database.get_activity(7, require_detail=True, now=later) is None
    # The summary row itself is still available; only the stale detail is withheld.
    assert database.get_activity(7, now=later) is not None
    database.put_activity_detail(detail, now=datetime(2026, 8, 21, 13, tzinfo=UTC))
    assert database.get_activity(7, require_detail=True, now=later) is not None


def test_activity_detail_round_trip_and_cache_marker(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    detail = ActivityDetail(
        summary=ActivitySummary(
            activity_id=123,
            start_time="2026-01-01 08:00:00",
            duration_seconds=1000,
            average_cadence=77,
        ),
        hr_zones_seconds={"zone_2": 500},
    )
    database.put_activity_detail(detail)
    cached = database.get_activity(123, require_detail=True)
    assert cached is not None
    assert cached.summary.average_cadence == 77
    assert cached.hr_zones_total_seconds == 500
    assert cached.hr_zone_coverage_percent == 50


def test_recovery_rows_preserve_sleep_and_hrv_without_daily_summary(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    database.put_sleep(SleepSummary(date="2026-01-01", sleep_score=80))
    database.put_hrv(HrvSummary(date="2026-01-01", nightly_average_ms=55))

    rows = database.recovery_rows("2026-01-01", "2026-01-01")
    assert len(rows) == 1
    assert rows[0]["sleep_score"] == 80
    assert rows[0]["nightly_avg_ms"] == 55
    assert rows[0]["resting_hr_bpm"] is None


def test_new_recovery_fields_survive_cache_round_trip(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    database.put_daily(
        DailySummary(
            date="2026-01-01",
            body_battery_at_wake=72,
            average_waking_respiration=14.2,
        ),
        TrainingReadiness(
            date="2026-01-01",
            hrv_factor_percent=86,
            hrv_factor_feedback="BALANCED",
        ),
    )
    database.put_sleep(
        SleepSummary(
            date="2026-01-01",
            average_hr_bpm=49,
            skin_temperature_deviation_c=0.31,
            body_battery_change=44,
        )
    )
    daily = database.get_daily("2026-01-01")
    readiness = database.get_readiness("2026-01-01")
    sleep = database.get_sleep("2026-01-01")
    assert daily is not None and daily.body_battery_at_wake == 72
    assert readiness is not None and readiness.hrv_factor_feedback == "BALANCED"
    assert sleep is not None and sleep.skin_temperature_deviation_c == 0.31
    assert sleep.body_battery_change == 44


def test_sqlite_file_contains_no_raw_json_payload(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    database.put_daily(DailySummary(date="2026-01-01", steps=1))
    # Sanity-check that the DB can be opened by stdlib SQLite and has normalized rows.
    connection = sqlite3.connect(database.path)
    try:
        assert connection.execute("SELECT steps FROM daily_metrics").fetchone() == (1,)
    finally:
        connection.close()


def test_schema_one_cache_migrates_to_cycle_schema(tmp_path: Path) -> None:
    path = tmp_path / "garmin.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()
    database = GarminDatabase(path)
    with database.connect() as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            migrated.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cycle_metrics'"
            ).fetchone()
            is not None
        )


def test_cached_training_load_keeps_the_unlabeled_code_warning(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    item = normalize_training_load({"trainingStatus": 7}, None, None, None, "2026-01-01")
    database.put_training_load(item)
    cached = database.get_training_load("2026-01-01")
    assert cached is not None
    assert cached.training_status_code == 7
    assert cached.training_status is None
    assert [notice.status for notice in cached.availability] == ["code_without_label"]


def test_unlabeled_status_is_not_recorded_as_an_unavailable_source(tmp_path: Path) -> None:
    """Only genuinely missing upstream reads belong in unavailable_sources."""
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    item = normalize_training_load(
        {"trainingStatus": 7}, None, None, None, "2026-01-01", ["hill_score"]
    )
    database.put_training_load(item)
    with database.connect() as connection:
        stored = connection.execute(
            "SELECT unavailable_sources FROM training_status WHERE date='2026-01-01'"
        ).fetchone()[0]
    assert stored == "hill_score"
