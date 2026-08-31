"""Normalized local SQLite history and cache.

Only explicit scalar fields are stored.  Raw Garmin responses and credentials never enter
this database.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast

from .models import (
    ActivityDetail,
    ActivityLap,
    ActivitySummary,
    AvailabilityNotice,
    BodyBatterySummary,
    BodyCompositionEntry,
    CacheInfo,
    CycleSummary,
    DailySummary,
    HrvSummary,
    SleepSummary,
    StressSummary,
    TrainingLoad,
    TrainingReadiness,
)
from .notices import (
    body_battery_notice,
    cycle_notices,
    unavailable_source_notice,
)

SCHEMA_VERSION = 3
TODAY_TTL = timedelta(minutes=20)
# A calendar day keeps changing after midnight: watches and scales upload late, and Garmin
# recomputes some daily aggregates. Treat a day as settled only at noon the following day, and
# trust a stored row indefinitely only when it was *fetched* after that moment.
DAY_SETTLES_AT = time(12, 0)
DEFAULT_DB_PATH = Path.home() / "Library/Application Support/garmin-owl/garmin.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
  date TEXT PRIMARY KEY, steps INTEGER, distance_m REAL, active_calories_kcal REAL,
  total_calories_kcal REAL, resting_hr_bpm INTEGER, min_hr_bpm INTEGER, max_hr_bpm INTEGER,
  average_stress INTEGER, max_stress INTEGER, body_battery_charged INTEGER,
  body_battery_drained INTEGER, floors_ascended REAL, floors_descended REAL,
  moderate_intensity_minutes INTEGER, vigorous_intensity_minutes INTEGER,
  training_readiness INTEGER, readiness_level TEXT, readiness_feedback TEXT,
  readiness_timestamp TEXT, recovery_time_minutes INTEGER, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sleep (
  date TEXT PRIMARY KEY, sleep_score INTEGER, total_sleep_seconds INTEGER,
  deep_sleep_seconds INTEGER, light_sleep_seconds INTEGER, rem_sleep_seconds INTEGER,
  awake_seconds INTEGER, sleep_start TEXT, sleep_end TEXT, respiration_avg REAL,
  respiration_min REAL, respiration_max REAL, spo2_avg REAL, spo2_min REAL,
  fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hrv (
  date TEXT PRIMARY KEY, status TEXT, nightly_avg_ms REAL, weekly_avg_ms REAL,
  last_night_avg_ms REAL, baseline_low_ms REAL, baseline_high_ms REAL,
  fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS training_status (
  date TEXT PRIMARY KEY, training_status TEXT, acute_load REAL, load_ratio REAL,
  vo2_max REAL, endurance_score REAL, hill_score REAL, unavailable_sources TEXT,
  fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS body_battery (
  date TEXT PRIMARY KEY, charged INTEGER, drained INTEGER, start_level INTEGER,
  end_level INTEGER, highest_level INTEGER, lowest_level INTEGER, data_status TEXT,
  fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stress (
  date TEXT PRIMARY KEY, average_stress INTEGER, max_stress INTEGER,
  stress_duration_seconds INTEGER, rest_duration_seconds INTEGER, low_duration_seconds INTEGER,
  medium_duration_seconds INTEGER, high_duration_seconds INTEGER, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
  activity_id INTEGER PRIMARY KEY, start_time TEXT, date TEXT, name TEXT, activity_type TEXT,
  duration_seconds REAL, elapsed_seconds REAL, distance_m REAL, calories_kcal REAL,
  average_hr_bpm REAL, max_hr_bpm REAL, average_speed_mps REAL, elevation_gain_m REAL,
  average_cadence REAL, average_power_w REAL, aerobic_training_effect REAL,
  anaerobic_training_effect REAL, training_effect_label TEXT, fetched_at TEXT NOT NULL,
  detail_fetched_at TEXT, hr_zones_status TEXT, power_zones_status TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
CREATE TABLE IF NOT EXISTS activity_laps (
  activity_id INTEGER NOT NULL, lap_index INTEGER NOT NULL, start_time TEXT,
  duration_seconds REAL, distance_m REAL, average_hr_bpm REAL, max_hr_bpm REAL,
  average_cadence REAL, average_power_w REAL, PRIMARY KEY(activity_id, lap_index),
  FOREIGN KEY(activity_id) REFERENCES activities(activity_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS activity_hr_zones (
  activity_id INTEGER NOT NULL, zone TEXT NOT NULL, seconds REAL NOT NULL,
  PRIMARY KEY(activity_id, zone),
  FOREIGN KEY(activity_id) REFERENCES activities(activity_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS activity_power_zones (
  activity_id INTEGER NOT NULL, zone TEXT NOT NULL, seconds REAL NOT NULL,
  PRIMARY KEY(activity_id, zone),
  FOREIGN KEY(activity_id) REFERENCES activities(activity_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS body_composition (
  timestamp TEXT PRIMARY KEY, weight_kg REAL, bmi REAL, body_fat_percent REAL,
  body_water_percent REAL, muscle_mass_kg REAL, bone_mass_kg REAL,
  visceral_fat_rating REAL, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_state (
  resource TEXT NOT NULL, key TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY(resource, key)
);
CREATE TABLE IF NOT EXISTS cycle_metrics (
  date TEXT PRIMARY KEY, phase TEXT, phase_code INTEGER, day_in_cycle INTEGER,
  cycle_start_date TEXT, period_length_days INTEGER, cycle_type TEXT,
  days_until_next_phase INTEGER, predicted_cycle_length_days INTEGER,
  fertile_window_start TEXT, fertile_window_end TEXT,
  next_predicted_cycle_start TEXT, data_status TEXT, fetched_at TEXT NOT NULL
);
"""

# Columns added after the first release of a table, applied to caches created earlier.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("training_status", "unavailable_sources", "TEXT"),
    ("cycle_metrics", "data_status", "TEXT"),
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(now: datetime | None = None) -> str:
    return (now or utc_now()).isoformat()


def _activity_date(item: ActivitySummary) -> str | None:
    return item.start_time[:10] if item.start_time and len(item.start_time) >= 10 else None


class GarminDatabase:
    """Small explicit repository around the normalized schema."""

    def __init__(self, path: Path | str | None = None) -> None:
        env_path = os.environ.get("GARMIN_OWL_DB")
        self.path = Path(path or env_path or DEFAULT_DB_PATH).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported garmin-owl cache schema {version}; expected {SCHEMA_VERSION}."
                )
            connection.executescript(SCHEMA)
            for table, column, column_type in ADDED_COLUMNS:
                existing = {
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
                    )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _upsert(self, table: str, key: str, values: Mapping[str, Any]) -> bool:
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != key)
        with self.connect() as connection:
            existed = connection.execute(
                f"SELECT 1 FROM {table} WHERE {key} = ?", (values[key],)
            ).fetchone()
            connection.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT({key}) DO UPDATE SET {updates}",
                tuple(values[column] for column in columns),
            )
        return existed is None

    def fetched_at(self, resource: str, key: str) -> datetime | None:
        table = {
            "daily": "daily_metrics",
            "sleep": "sleep",
            "hrv": "hrv",
            "cycle": "cycle_metrics",
            "body_battery": "body_battery",
            "stress": "stress",
        }.get(resource)
        with self.connect() as connection:
            if table:
                row = connection.execute(
                    f"SELECT fetched_at FROM {table} WHERE date = ?", (key,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT fetched_at FROM sync_state WHERE resource = ? AND key = ?",
                    (resource, key),
                ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    @staticmethod
    def settled_at(target: date, tzinfo: Any) -> datetime:
        """Local moment after which ``target`` can no longer gain late device uploads."""
        return datetime.combine(target + timedelta(days=1), DAY_SETTLES_AT, tzinfo=tzinfo)

    def is_current(self, target: date, fetched: datetime, current: datetime) -> bool:
        """Decide whether a row captured at ``fetched`` still represents ``target`` fully.

        A row is authoritative only if it was captured after its day settled.  A row captured
        earlier may have observed a partially synchronized day, so it is reused only briefly
        and is re-fetched once the day has settled.
        """
        comparable = fetched.astimezone(current.tzinfo)
        settled = self.settled_at(target, current.tzinfo)
        if comparable >= settled:
            return True
        return current < settled and current - comparable < TODAY_TTL

    def is_fresh(
        self, resource: str, key: str, *, now: datetime | None = None, force: bool = False
    ) -> bool:
        if force:
            return False
        fetched = self.fetched_at(resource, key)
        if fetched is None:
            return False
        current = now or datetime.now().astimezone()
        return self.is_current(date.fromisoformat(key), fetched, current)

    def mark_synced(self, resource: str, key: str, *, now: datetime | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO sync_state(resource,key,fetched_at) VALUES(?,?,?) "
                "ON CONFLICT(resource,key) DO UPDATE SET fetched_at=excluded.fetched_at",
                (resource, key, _timestamp(now)),
            )

    def is_range_fresh(
        self,
        resource: str,
        start: str,
        end: str,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> bool:
        if force:
            return False
        fetched = self.fetched_at(resource, f"{start}:{end}")
        if fetched is None:
            return False
        current = now or datetime.now().astimezone()
        # The range is only as settled as its last day; a range captured mid-window may be
        # missing activities that synchronized afterwards.
        return self.is_current(date.fromisoformat(end), fetched, current)

    def is_activity_range_fresh(
        self,
        start: str,
        end: str,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> bool:
        return self.is_range_fresh("activities", start, end, now=now, force=force)

    def put_daily(
        self,
        item: DailySummary,
        readiness: TrainingReadiness | None = None,
        *,
        now: datetime | None = None,
    ) -> bool:
        return self._upsert(
            "daily_metrics",
            "date",
            {
                "date": item.date,
                "steps": item.steps,
                "distance_m": item.distance_m,
                "active_calories_kcal": item.active_calories_kcal,
                "total_calories_kcal": item.total_calories_kcal,
                "resting_hr_bpm": item.resting_hr_bpm,
                "min_hr_bpm": item.min_hr_bpm,
                "max_hr_bpm": item.max_hr_bpm,
                "average_stress": item.average_stress,
                "max_stress": item.max_stress,
                "body_battery_charged": item.body_battery_charged,
                "body_battery_drained": item.body_battery_drained,
                "floors_ascended": item.floors_ascended,
                "floors_descended": item.floors_descended,
                "moderate_intensity_minutes": item.moderate_intensity_minutes,
                "vigorous_intensity_minutes": item.vigorous_intensity_minutes,
                "training_readiness": readiness.score if readiness else None,
                "readiness_level": readiness.level if readiness else None,
                "readiness_feedback": readiness.feedback if readiness else None,
                "readiness_timestamp": readiness.timestamp if readiness else None,
                "recovery_time_minutes": readiness.recovery_time_minutes if readiness else None,
                "fetched_at": _timestamp(now),
            },
        )

    def get_daily(self, cdate: str) -> DailySummary | None:
        row = self._row("daily_metrics", "date", cdate)
        return DailySummary(**dict(row)) if row else None

    def get_readiness(self, cdate: str) -> TrainingReadiness | None:
        row = self._row("daily_metrics", "date", cdate)
        if not row:
            return None
        return TrainingReadiness(
            date=cdate,
            score=row["training_readiness"],
            level=row["readiness_level"],
            feedback=row["readiness_feedback"],
            timestamp=row["readiness_timestamp"],
            recovery_time_minutes=row["recovery_time_minutes"],
        )

    def put_sleep(self, item: SleepSummary, *, now: datetime | None = None) -> bool:
        return self._upsert(
            "sleep",
            "date",
            {
                "date": item.date,
                "sleep_score": item.sleep_score,
                "total_sleep_seconds": item.total_sleep_seconds,
                "deep_sleep_seconds": item.deep_sleep_seconds,
                "light_sleep_seconds": item.light_sleep_seconds,
                "rem_sleep_seconds": item.rem_sleep_seconds,
                "awake_seconds": item.awake_seconds,
                "sleep_start": item.sleep_start,
                "sleep_end": item.sleep_end,
                "respiration_avg": item.average_respiration,
                "respiration_min": item.lowest_respiration,
                "respiration_max": item.highest_respiration,
                "spo2_avg": item.average_spo2_percent,
                "spo2_min": item.lowest_spo2_percent,
                "fetched_at": _timestamp(now),
            },
        )

    def get_sleep(self, cdate: str) -> SleepSummary | None:
        row = self._row("sleep", "date", cdate)
        if not row:
            return None
        data = dict(row)
        aliases = {
            "respiration_avg": "average_respiration",
            "respiration_min": "lowest_respiration",
            "respiration_max": "highest_respiration",
            "spo2_avg": "average_spo2_percent",
            "spo2_min": "lowest_spo2_percent",
        }
        for old, new in aliases.items():
            data[new] = data.pop(old)
        return SleepSummary(**data)

    def put_hrv(self, item: HrvSummary, *, now: datetime | None = None) -> bool:
        return self._upsert(
            "hrv",
            "date",
            {
                "date": item.date,
                "status": item.status,
                "nightly_avg_ms": item.nightly_average_ms,
                "weekly_avg_ms": item.weekly_average_ms,
                "last_night_avg_ms": item.last_night_average_ms,
                "baseline_low_ms": item.baseline_low_ms,
                "baseline_high_ms": item.baseline_high_ms,
                "fetched_at": _timestamp(now),
            },
        )

    def get_hrv(self, cdate: str) -> HrvSummary | None:
        row = self._row("hrv", "date", cdate)
        if not row:
            return None
        data = dict(row)
        aliases = {
            "nightly_avg_ms": "nightly_average_ms",
            "weekly_avg_ms": "weekly_average_ms",
            "last_night_avg_ms": "last_night_average_ms",
        }
        for old, new in aliases.items():
            data[new] = data.pop(old)
        return HrvSummary(**data)

    def put_training_load(self, item: TrainingLoad, *, now: datetime | None = None) -> bool:
        # Availability notices are regenerated on read from the source names, so that a cached
        # row cannot silently lose the record of which Garmin reads were unavailable.
        unavailable = sorted(
            {notice.field for notice in item.availability if notice.status != "available"}
        )
        return self._upsert(
            "training_status",
            "date",
            {
                **item.model_dump(exclude={"availability"}),
                "unavailable_sources": ",".join(unavailable) or None,
                "fetched_at": _timestamp(now),
            },
        )

    def get_training_load(self, cdate: str) -> TrainingLoad | None:
        row = self._row("training_status", "date", cdate)
        if not row:
            return None
        data = dict(row)
        unavailable = str(data.pop("unavailable_sources") or "")
        return TrainingLoad(
            **data,
            availability=[
                unavailable_source_notice(source)
                for source in unavailable.split(",")
                if source
            ],
        )

    def put_body_battery(
        self, item: BodyBatterySummary, *, now: datetime | None = None
    ) -> bool:
        return self._upsert(
            "body_battery",
            "date",
            {
                "date": item.date,
                "charged": item.charged,
                "drained": item.drained,
                "start_level": item.start_level,
                "end_level": item.end_level,
                "highest_level": item.highest_level,
                "lowest_level": item.lowest_level,
                "data_status": next(
                    (notice.status for notice in item.availability), None
                ),
                "fetched_at": _timestamp(now),
            },
        )

    def get_body_battery(self, cdate: str) -> BodyBatterySummary | None:
        row = self._row("body_battery", "date", cdate)
        if not row:
            return None
        data = dict(row)
        status = data.pop("data_status")
        return BodyBatterySummary(
            **data,
            availability=(
                [body_battery_notice(str(status), cdate)] if status else []
            ),
        )

    def put_stress(self, item: StressSummary, *, now: datetime | None = None) -> bool:
        return self._upsert(
            "stress",
            "date",
            {
                **item.model_dump(exclude={"timeseries", "availability"}),
                "fetched_at": _timestamp(now),
            },
        )

    def get_stress(self, cdate: str) -> StressSummary | None:
        row = self._row("stress", "date", cdate)
        return StressSummary(**dict(row)) if row else None

    def put_activity_summary(
        self, item: ActivitySummary, *, now: datetime | None = None
    ) -> bool:
        values = item.model_dump()
        values.update({"date": _activity_date(item), "fetched_at": _timestamp(now)})
        columns = list(values)
        with self.connect() as connection:
            existed = connection.execute(
                "SELECT 1 FROM activities WHERE activity_id=?", (item.activity_id,)
            ).fetchone()
            updates = ", ".join(
                f"{column}=COALESCE(excluded.{column},activities.{column})"
                for column in columns
                if column != "activity_id"
            )
            connection.execute(
                f"INSERT INTO activities ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)}) "
                f"ON CONFLICT(activity_id) DO UPDATE SET {updates}",
                tuple(values[column] for column in columns),
            )
        return existed is None

    def put_activity_detail(
        self, item: ActivityDetail, *, now: datetime | None = None
    ) -> bool:
        inserted = self.put_activity_summary(item.summary, now=now)
        stamp = _timestamp(now)
        with self.connect() as connection:
            connection.execute(
                "UPDATE activities SET aerobic_training_effect=?, anaerobic_training_effect=?, "
                "training_effect_label=?, detail_fetched_at=?, hr_zones_status=?, "
                "power_zones_status=? WHERE activity_id=?",
                (
                    item.training_effect_aerobic,
                    item.training_effect_anaerobic,
                    item.training_effect_label,
                    stamp,
                    next(
                        (
                            notice.status
                            for notice in item.availability
                            if notice.field == "hr_zones_seconds"
                        ),
                        "available",
                    ),
                    next(
                        (
                            notice.status
                            for notice in item.availability
                            if notice.field == "power_zones_seconds"
                        ),
                        "available",
                    ),
                    item.summary.activity_id,
                ),
            )
            connection.execute(
                "DELETE FROM activity_laps WHERE activity_id=?", (item.summary.activity_id,)
            )
            connection.executemany(
                "INSERT INTO activity_laps VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        item.summary.activity_id,
                        lap.lap_index if lap.lap_index is not None else index,
                        lap.start_time,
                        lap.duration_seconds,
                        lap.distance_m,
                        lap.average_hr_bpm,
                        lap.max_hr_bpm,
                        lap.average_cadence,
                        lap.average_power_w,
                    )
                    for index, lap in enumerate(item.laps, 1)
                ],
            )
            for table, zones in (
                ("activity_hr_zones", item.hr_zones_seconds),
                ("activity_power_zones", item.power_zones_seconds),
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE activity_id=?", (item.summary.activity_id,)
                )
                connection.executemany(
                    f"INSERT INTO {table}(activity_id,zone,seconds) VALUES(?,?,?)",
                    [(item.summary.activity_id, zone, seconds) for zone, seconds in zones.items()],
                )
        return inserted

    def get_activity(
        self,
        activity_id: int,
        *,
        require_detail: bool = False,
        now: datetime | None = None,
    ) -> ActivityDetail | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM activities WHERE activity_id = ?", (activity_id,)
            ).fetchone()
            if not row:
                return None
            if require_detail and not self._detail_is_current(row, now):
                # Detail captured while the activity's day was still synchronizing can be
                # missing laps or zone time that arrived later, and Garmin lets activities be
                # edited afterwards.  Re-fetch instead of serving it forever.
                return None
            lap_rows = connection.execute(
                "SELECT * FROM activity_laps WHERE activity_id=? ORDER BY lap_index", (activity_id,)
            ).fetchall()
            hr = connection.execute(
                "SELECT zone,seconds FROM activity_hr_zones WHERE activity_id=?", (activity_id,)
            ).fetchall()
            power = connection.execute(
                "SELECT zone,seconds FROM activity_power_zones WHERE activity_id=?", (activity_id,)
            ).fetchall()
        summary = ActivitySummary(**dict(row))
        zones = {str(item["zone"]): float(item["seconds"]) for item in hr}
        total = sum(zones.values()) or None
        coverage = (
            round(total / summary.duration_seconds * 100, 1)
            if total is not None and summary.duration_seconds
            else None
        )
        return ActivityDetail(
            summary=summary,
            training_effect_aerobic=row["aerobic_training_effect"],
            training_effect_anaerobic=row["anaerobic_training_effect"],
            training_effect_label=row["training_effect_label"],
            laps=[ActivityLap(**dict(item)) for item in lap_rows],
            hr_zones_seconds=zones,
            power_zones_seconds={str(item["zone"]): float(item["seconds"]) for item in power},
            hr_zones_total_seconds=total,
            activity_duration_seconds=summary.duration_seconds,
            hr_zone_coverage_percent=coverage,
            availability=[
                AvailabilityNotice(
                    field=field,
                    status=str(status),
                    message=f"Garmin did not provide activity {label} aggregates.",
                )
                for field, status, label in (
                    ("hr_zones_seconds", row["hr_zones_status"], "HR-zone"),
                    ("power_zones_seconds", row["power_zones_status"], "power-zone"),
                )
                if status not in (None, "available")
            ],
        )

    def _detail_is_current(self, row: sqlite3.Row, now: datetime | None) -> bool:
        stamp = row["detail_fetched_at"]
        if stamp is None:
            return False
        activity_date = row["date"]
        if activity_date is None:
            return True
        return self.is_current(
            date.fromisoformat(str(activity_date)),
            datetime.fromisoformat(str(stamp)),
            now or datetime.now().astimezone(),
        )

    def list_activities(
        self,
        start_date: str,
        end_date: str,
        *,
        limit: int = 100,
        activity_type: str | None = None,
    ) -> list[ActivitySummary]:
        sql = "SELECT * FROM activities WHERE date BETWEEN ? AND ?"
        parameters: list[Any] = [start_date, end_date]
        if activity_type:
            sql += " AND lower(activity_type)=lower(?)"
            parameters.append(activity_type)
        sql += " ORDER BY start_time DESC, activity_id DESC LIMIT ?"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [ActivitySummary(**dict(row)) for row in rows]

    def weekly_activity_aggregates(
        self, start_date: str, end_date: str
    ) -> tuple[dict[str, float], float | None, float | None, int, int, int]:
        """Return detail-backed weekly aggregates together with their activity coverage."""
        with self.connect() as connection:
            zone_rows = connection.execute(
                "SELECT z.zone AS zone, SUM(z.seconds) AS seconds FROM activity_hr_zones z "
                "JOIN activities a ON a.activity_id = z.activity_id "
                "WHERE a.date BETWEEN ? AND ? GROUP BY z.zone",
                (start_date, end_date),
            ).fetchall()
            effect_row = connection.execute(
                "SELECT MAX(aerobic_training_effect) AS aerobic, "
                "MAX(anaerobic_training_effect) AS anaerobic FROM activities "
                "WHERE date BETWEEN ? AND ?",
                (start_date, end_date),
            ).fetchone()
            coverage_row = connection.execute(
                "SELECT "
                "COUNT(CASE WHEN detail_fetched_at IS NOT NULL THEN 1 END) AS detail_count, "
                "COUNT(CASE WHEN aerobic_training_effect IS NOT NULL "
                "OR anaerobic_training_effect IS NOT NULL THEN 1 END) AS effect_count, "
                "COUNT(CASE WHEN hr_zones_status='available' THEN 1 END) AS hr_count "
                "FROM activities WHERE date BETWEEN ? AND ?",
                (start_date, end_date),
            ).fetchone()
        zones = {str(row["zone"]): float(row["seconds"]) for row in zone_rows}
        aerobic = effect_row["aerobic"] if effect_row else None
        anaerobic = effect_row["anaerobic"] if effect_row else None
        return (
            zones,
            aerobic,
            anaerobic,
            int(coverage_row["detail_count"]) if coverage_row else 0,
            int(coverage_row["effect_count"]) if coverage_row else 0,
            int(coverage_row["hr_count"]) if coverage_row else 0,
        )

    def recovery_rows(self, start_date: str, end_date: str) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "WITH dates AS (SELECT date FROM daily_metrics UNION SELECT date FROM sleep "
                "UNION SELECT date FROM hrv) "
                "SELECT dates.date,d.resting_hr_bpm,d.training_readiness,d.recovery_time_minutes,"
                "s.sleep_score,h.nightly_avg_ms FROM dates "
                "LEFT JOIN daily_metrics d ON d.date=dates.date "
                "LEFT JOIN sleep s ON s.date=dates.date LEFT JOIN hrv h ON h.date=dates.date "
                "WHERE dates.date BETWEEN ? AND ? ORDER BY dates.date",
                (start_date, end_date),
            ).fetchall()

    def put_body_composition(
        self, items: list[BodyCompositionEntry], *, now: datetime | None = None
    ) -> tuple[int, int]:
        inserted = updated = 0
        for item in items:
            if item.timestamp is None:
                continue
            created = self._upsert(
                "body_composition",
                "timestamp",
                {**item.model_dump(), "fetched_at": _timestamp(now)},
            )
            inserted += int(created)
            updated += int(not created)
        return inserted, updated

    def get_body_composition(
        self, start_date: str, end_date: str
    ) -> list[BodyCompositionEntry]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM body_composition WHERE substr(timestamp,1,10) BETWEEN ? AND ? "
                "ORDER BY timestamp",
                (start_date, end_date),
            ).fetchall()
        return [BodyCompositionEntry(**dict(row)) for row in rows]

    def put_cycle(self, item: CycleSummary, *, now: datetime | None = None) -> bool:
        # ``availability`` carries the "Garmin has no cycle data here" and "this field is a
        # garmin-owl derivation" notices.  Persist the source status so a cache hit cannot
        # present an empty row as if Garmin had simply returned nothing of interest.
        return self._upsert(
            "cycle_metrics",
            "date",
            {
                **item.model_dump(exclude={"availability"}),
                "data_status": next(
                    (
                        notice.status
                        for notice in item.availability
                        if notice.field == "cycle"
                    ),
                    "available",
                ),
                "fetched_at": _timestamp(now),
            },
        )

    def get_cycle(self, cdate: str) -> CycleSummary | None:
        row = self._row("cycle_metrics", "date", cdate)
        if not row:
            return None
        data = dict(row)
        status = str(data.pop("data_status") or "available")
        return CycleSummary(**data, availability=cycle_notices(status, cdate))

    def _row(self, table: str, key: str, value: Any) -> sqlite3.Row | None:
        with self.connect() as connection:
            return cast(
                sqlite3.Row | None,
                connection.execute(
                f"SELECT * FROM {table} WHERE {key} = ?", (value,)
                ).fetchone(),
            )

    def info(self) -> CacheInfo:
        tables = (
            "daily_metrics", "sleep", "hrv", "training_status", "activities",
            "activity_laps", "activity_hr_zones", "activity_power_zones", "body_composition",
            "cycle_metrics", "body_battery", "stress",
        )
        with self.connect() as connection:
            counts = {
                table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                for table in tables
            }
            dates = connection.execute(
                "SELECT min(date),max(date) FROM daily_metrics"
            ).fetchone()
        return CacheInfo(
            path=str(self.path),
            schema_version=SCHEMA_VERSION,
            size_bytes=self.path.stat().st_size if self.path.exists() else 0,
            first_date=dates[0] if dates else None,
            last_date=dates[1] if dates else None,
            table_rows=counts,
        )

    def clear(self, *, vacuum: bool = False) -> None:
        tables = (
            "activity_laps", "activity_hr_zones", "activity_power_zones", "activities",
            "daily_metrics", "sleep", "hrv", "training_status", "body_composition", "sync_state",
            "cycle_metrics", "body_battery", "stress",
        )
        with self.connect() as connection:
            for table in tables:
                connection.execute(f"DELETE FROM {table}")
            if vacuum:
                connection.execute("VACUUM")
