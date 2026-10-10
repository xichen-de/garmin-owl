from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from garmin_owl import database as database_module
from garmin_owl.database import SCHEMA_VERSION, GarminDatabase, GarminOwlCacheError
from garmin_owl.models import (
    ActivityDetail,
    ActivityLap,
    ActivitySummary,
    BodyCompositionEntry,
    DailySummary,
    HrvSummary,
    SleepSummary,
    TrainingReadiness,
)
from garmin_owl.normalize import normalize_training_load


@pytest.mark.parametrize("xdg", [None, "", "relative/path", "~/data", "/custom/data"])
def test_linux_default_cache_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, xdg: str | None
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    if xdg is not None:
        monkeypatch.setenv("XDG_DATA_HOME", xdg)
    base = Path("/custom/data") if xdg == "/custom/data" else tmp_path / ".local/share"
    assert database_module.default_db_path() == base / "garmin-owl/garmin.sqlite"


def test_macos_cache_path_is_preserved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", "/ignored")
    assert database_module.default_db_path() == (
        tmp_path / "Library/Application Support/garmin-owl/garmin.sqlite"
    )


def test_linux_database_path_precedence_and_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GARMIN_OWL_DB", raising=False)
    database = GarminDatabase()
    assert database.path == tmp_path / "data/garmin-owl/garmin.sqlite"
    assert database.path.stat().st_mode & 0o077 == 0
    assert database.path.parent.stat().st_mode & 0o077 == 0
    monkeypatch.setenv("GARMIN_OWL_DB", "~/override/garmin.sqlite")
    assert GarminDatabase().path == tmp_path / "override/garmin.sqlite"
    explicit = tmp_path / "explicit/garmin.sqlite"
    assert GarminDatabase(explicit).path == explicit


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


def test_custom_database_preserves_existing_parent_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    original_mode = parent.stat().st_mode
    database = GarminDatabase(parent / "garmin.sqlite")
    assert parent.stat().st_mode == original_mode
    assert database.path.stat().st_mode & 0o077 == 0


def test_clear_with_vacuum_commits_deletions(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    database.put_daily(DailySummary(date="2026-01-01", steps=100))
    database.mark_synced("readiness", "2026-01-01")
    database.clear(vacuum=True)
    assert database.get_daily("2026-01-01") is None
    assert database.fetched_at("readiness", "2026-01-01") is None
    with database.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_activity_note_round_trips_and_list_reads_never_erase_it(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    note = "Wall balls 9 kg\n3 Runden · Griff müde 💪"
    summary = ActivitySummary(activity_id=5, start_time="2026-08-20 08:00:00", description=note)
    database.put_activity_detail(ActivityDetail(summary=summary))
    # A later list read that omits the note must not clear the stored one.
    database.put_activity_summary(summary.model_copy(update={"description": None}))
    cached = database.get_activity(5)
    assert cached is not None and cached.summary.description == note
    assert database.list_activities("2026-08-20", "2026-08-20")[0].description == note
    # An edited note arriving through any read replaces the stored one.
    database.put_activity_summary(summary.model_copy(update={"description": "edited"}))
    cached = database.get_activity(5)
    assert cached is not None and cached.summary.description == "edited"
    # A detail read is authoritative, so a note deleted in Garmin Connect disappears.
    database.put_activity_detail(
        ActivityDetail(summary=summary.model_copy(update={"description": None}))
    )
    cached = database.get_activity(5)
    assert cached is not None and cached.summary.description is None


def test_schema_five_cache_gains_notes_and_rereads_activities_once(tmp_path: Path) -> None:
    path = tmp_path / "garmin.sqlite"
    settled = datetime(2026, 8, 25, 9, tzinfo=UTC)
    database = GarminDatabase(path)
    database.put_activity_detail(
        ActivityDetail(
            summary=ActivitySummary(
                activity_id=7, start_time="2026-08-20 08:00:00", duration_seconds=600
            )
        ),
        now=settled,
    )
    database.mark_synced("activities", "2026-08-20:2026-08-20", now=settled)
    database.close()
    # Recreate a version-5 cache: same rows, but no description column.
    connection = sqlite3.connect(path)
    try:
        connection.execute("ALTER TABLE activities DROP COLUMN description")
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
    finally:
        connection.close()

    database = GarminDatabase(path)
    later = datetime(2026, 9, 1, 9, tzinfo=UTC)
    with database.connect() as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(activities)")}
        assert "description" in columns
        # The detail stays counted as detail-backed for weekly coverage.
        assert migrated.execute("SELECT detail_fetched_at FROM activities").fetchone()[0]
    kept = database.get_activity(7)
    assert kept is not None and kept.summary.duration_seconds == 600
    # Settled rows from before the upgrade never saw notes, so they are re-read once.
    assert database.get_activity(7, require_detail=True, now=later) is None
    assert not database.is_activity_range_fresh("2026-08-20", "2026-08-20", now=later)
    database.close()
    # Reopening an already-migrated cache does not invalidate anything again.
    database = GarminDatabase(path)
    database.put_activity_detail(
        ActivityDetail(summary=ActivitySummary(activity_id=7, start_time="2026-08-20 08:00:00")),
        now=settled,
    )
    database.close()
    database = GarminDatabase(path)
    assert database.get_activity(7, require_detail=True, now=later) is not None


def test_cached_zone_total_is_rounded_like_a_live_read(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    zones = {"zone_1": 0.1, "zone_2": 0.2}  # sums to 0.30000000000000004 unrounded
    database.put_activity_detail(
        ActivityDetail(
            summary=ActivitySummary(activity_id=3, start_time="2026-08-20 08:00:00"),
            hr_zones_seconds=zones,
        )
    )
    cached = database.get_activity(3)
    assert cached is not None and cached.hr_zones_total_seconds == 0.3


def test_activity_ratings_survive_list_reads_and_clear_on_detail_reads(tmp_path: Path) -> None:
    database = GarminDatabase(tmp_path / "garmin.sqlite")
    rated = ActivitySummary(
        activity_id=5, start_time="2026-08-20 08:00:00", perceived_effort=4, feel="normal"
    )
    database.put_activity_detail(ActivityDetail(summary=rated))
    # Garmin's activity list never carries ratings; a list read must not erase them.
    database.put_activity_summary(ActivitySummary(activity_id=5, start_time=rated.start_time))
    cached = database.get_activity(5)
    assert cached is not None
    assert (cached.summary.perceived_effort, cached.summary.feel) == (4, "normal")
    database.put_activity_detail(
        ActivityDetail(summary=ActivitySummary(activity_id=5, start_time=rated.start_time))
    )
    cached = database.get_activity(5)
    assert cached is not None
    assert (cached.summary.perceived_effort, cached.summary.feel) == (None, None)


def test_account_binding_discards_unowned_rows_and_rejects_other_accounts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cache.sqlite"
    database = GarminDatabase(path)
    database.put_daily(DailySummary(date="2026-01-01", steps=42))
    database.put_activity_detail(
        ActivityDetail(
            summary=ActivitySummary(activity_id=1, start_time="2026-01-01T08:00:00+01:00"),
            laps=[ActivityLap(lap_index=1)],
            hr_zones_seconds={"zone_1": 60},
        )
    )
    database.mark_synced("activities", "2026-01-01:2026-01-01")
    # Rows cached before accounts were recorded cannot be attributed: they are dropped, not
    # served, and no longer block every read until the user clears the cache by hand.
    database.bind_account("account-a")
    assert database.get_daily("2026-01-01") is None
    assert database.get_activity(1) is None
    assert not database.is_activity_range_fresh("2026-01-01", "2026-01-01")
    database.put_daily(DailySummary(date="2026-01-01", steps=42))
    database.close()
    reopened = GarminDatabase(path)
    reopened.bind_account("account-a")
    with pytest.raises(RuntimeError, match="another Garmin account"):
        reopened.bind_account("account-b")
    assert reopened.get_daily("2026-01-01") is not None


def test_range_replacement_rolls_back_deletions_and_partial_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = GarminDatabase(tmp_path / "cache.sqlite")
    database.put_activity_summary(ActivitySummary(activity_id=1, start_time="2026-01-01"))
    original = database.put_activity_summary

    def fail_after_write(item: ActivitySummary) -> bool:
        original(item)
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(database, "put_activity_summary", fail_after_write)
    with pytest.raises(RuntimeError, match="simulated"):
        database.replace_activities(
            "2026-01-01", "2026-01-01", [ActivitySummary(activity_id=2, start_time="2026-01-01")]
        )
    assert database.get_activity(1) is not None
    assert database.get_activity(2) is None
    assert not database.is_activity_range_fresh("2026-01-01", "2026-01-01")


def test_schema_seven_cache_drops_ambiguous_timestamps_and_rereads_them(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    database = GarminDatabase(path)
    database.bind_account("account-a")
    # Version 7 stored Garmin's local wall time encoded as if it were UTC.
    database.put_sleep(SleepSummary(date="2026-10-10", sleep_score=80, sleep_start="1791587896000"))
    database.put_daily(
        DailySummary(date="2026-10-10", steps=1),
        TrainingReadiness(date="2026-10-10", score=70, timestamp="2026-10-10T07:00:29.0"),
    )
    database.mark_synced("readiness", "2026-10-10")
    database.put_activity_detail(
        ActivityDetail(summary=ActivitySummary(activity_id=1, start_time="2026-10-10 07:30:00"))
    )
    database.mark_synced("activities", "2026-10-10:2026-10-10")
    database.put_body_composition([BodyCompositionEntry(timestamp="2026-10-10T07:00:00")])
    database.mark_synced("body_composition", "2026-10-10:2026-10-10")
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 7")

    migrated = GarminDatabase(path)
    later = datetime(2026, 10, 12, 12, tzinfo=UTC)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    sleep = migrated.get_sleep("2026-10-10")
    assert sleep is not None and sleep.sleep_score == 80 and sleep.sleep_start is None
    assert not migrated.is_fresh("sleep", "2026-10-10", now=later)
    readiness = migrated.get_readiness("2026-10-10")
    assert readiness is not None and readiness.timestamp is None
    assert not migrated.is_fresh("readiness", "2026-10-10", now=later)
    assert migrated.get_activity(1, require_detail=True, now=later) is None
    assert not migrated.is_activity_range_fresh("2026-10-10", "2026-10-10", now=later)
    assert migrated.get_body_composition("2026-10-10", "2026-10-10") == []
    assert not migrated.is_range_fresh("body_composition", "2026-10-10", "2026-10-10", now=later)
    # The account binding survives a format migration.
    migrated.bind_account("account-a")


def test_newer_schema_cache_is_kept_aside_instead_of_failing_every_read(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    database = GarminDatabase(path)
    database.put_daily(DailySummary(date="2026-10-10", steps=7))
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    fresh = GarminDatabase(path)
    assert fresh.get_daily("2026-10-10") is None
    fresh.put_daily(DailySummary(date="2026-10-10", steps=8))
    kept = tmp_path / f"cache.sqlite.schema-{SCHEMA_VERSION + 1}"
    with sqlite3.connect(kept) as connection:
        assert connection.execute("SELECT steps FROM daily_metrics").fetchone()[0] == 7


def test_version_is_rechecked_on_every_transaction(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    database = GarminDatabase(path)
    database.put_daily(DailySummary(date="2026-10-10", steps=7))
    with sqlite3.connect(path) as other_process:
        other_process.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(GarminOwlCacheError, match=r"newer garmin-owl.*Restart"):
        database.get_daily("2026-10-10")
    with sqlite3.connect(path) as other_process:
        other_process.execute("PRAGMA user_version = 7")
    # An older layout written underneath a running server is migrated before it is read.
    assert database.get_daily("2026-10-10") is not None
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


@pytest.mark.parametrize(
    ("table", "read"),
    [
        ("sleep", lambda database: database.get_sleep("2026-10-10")),
        ("daily_metrics", lambda database: database.get_daily("2026-10-10")),
        ("hrv", lambda database: database.get_hrv("2026-10-10")),
    ],
)
def test_unreadable_cached_row_is_a_cache_miss_not_a_crash(
    tmp_path: Path, table: str, read: Any
) -> None:
    database = GarminDatabase(tmp_path / "cache.sqlite")
    with database.connect() as connection:
        # A value no current model accepts, standing in for any future format drift.
        connection.execute(
            f"INSERT INTO {table}(date, fetched_at) VALUES('2026-10-10', 'not-a-timestamp')"
        )
        column = {"sleep": "sleep_score", "daily_metrics": "steps", "hrv": "nightly_avg_ms"}[table]
        connection.execute(f"UPDATE {table} SET {column}='unreadable'")
    assert read(database) is None
    with database.connect() as connection:
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_schema_eight_cache_corrects_units_zeros_and_rounding_in_place(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    database = GarminDatabase(path)
    database.put_activity_detail(
        ActivityDetail(
            summary=ActivitySummary(
                activity_id=1,
                start_time="2026-10-05T08:00:00+02:00",
                average_stride_length_m=72.606,
                distance_m=0.0,
                average_speed_mps=0.0,
                average_respiration=26.940000534057617,
            )
        )
    )
    database.put_sleep(SleepSummary(date="2026-10-07", skin_temperature_deviation_c=-0.0))
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 8")

    migrated = GarminDatabase(path)
    activity = migrated.get_activity(1)
    assert activity is not None
    summary = activity.summary
    assert summary.average_stride_length_m == pytest.approx(0.72606)
    assert (summary.distance_m, summary.average_speed_mps) == (None, None)
    assert summary.average_respiration == 26.9
    sleep = migrated.get_sleep("2026-10-07")
    assert sleep is not None and str(sleep.skin_temperature_deviation_c) == "0.0"
