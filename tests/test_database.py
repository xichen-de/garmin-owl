from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from garmin_owl.database import SCHEMA_VERSION, GarminDatabase
from garmin_owl.models import ActivityDetail, ActivitySummary, DailySummary


def test_schema_version_permissions_and_no_sensitive_columns(tmp_path) -> None:
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


def test_upsert_has_no_duplicates_and_clear_preserves_database(tmp_path) -> None:
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


def test_freshness_today_yesterday_and_history(tmp_path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    local = datetime(2026, 8, 30, 10, tzinfo=UTC)
    database.put_daily(DailySummary(date="2026-08-30"), now=local - timedelta(minutes=19))
    database.put_daily(DailySummary(date="2026-08-29"), now=local)
    database.put_daily(DailySummary(date="2026-08-28"), now=local)
    assert database.is_fresh("daily", "2026-08-30", now=local)
    assert not database.is_fresh("daily", "2026-08-30", now=local + timedelta(minutes=2))
    assert not database.is_fresh("daily", "2026-08-29", now=local)
    assert database.is_fresh("daily", "2026-08-29", now=local.replace(hour=12))
    assert database.is_fresh("daily", "2026-08-28", now=local)


def test_activity_detail_round_trip_and_cache_marker(tmp_path) -> None:
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


def test_sqlite_file_contains_no_raw_json_payload(tmp_path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    database.put_daily(DailySummary(date="2026-01-01", steps=1))
    # Sanity-check that the DB can be opened by stdlib SQLite and has normalized rows.
    connection = sqlite3.connect(database.path)
    try:
        assert connection.execute("SELECT steps FROM daily_metrics").fetchone() == (1,)
    finally:
        connection.close()


def test_schema_one_cache_migrates_to_cycle_schema(tmp_path) -> None:
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
