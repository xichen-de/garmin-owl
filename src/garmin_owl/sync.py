"""Incremental normalized cache synchronization, independent of MCP transport."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from .client import GarminDataClient, GarminOwlError
from .database import GarminDatabase
from .models import SyncReport
from .normalize import (
    normalize_activities,
    normalize_daily_summary,
    normalize_hrv,
    normalize_sleep,
    normalize_training_load,
    normalize_training_readiness,
)

MAX_SYNC_DAYS = 366


def dates_between(start: date, end: date) -> list[str]:
    if start > end:
        raise ValueError("start must be on or before end")
    count = (end - start).days + 1
    if count > MAX_SYNC_DAYS:
        raise ValueError(f"sync range cannot exceed {MAX_SYNC_DAYS} days")
    return [(start + timedelta(days=offset)).isoformat() for offset in range(count)]


class SyncEngine:
    def __init__(self, client: GarminDataClient, database: GarminDatabase) -> None:
        self.client = client
        self.database = database

    def ensure_resource(self, resource: str, cdate: str, *, force: bool = False) -> bool:
        """Ensure one normalized resource exists; return whether Garmin was called."""
        if self.database.is_fresh(resource, cdate, force=force):
            return False
        if resource == "daily":
            current = self.database.get_readiness(cdate)
            item = normalize_daily_summary(self.client.daily_summary(cdate), cdate)
            self.database.put_daily(item, current)
        elif resource == "sleep":
            self.database.put_sleep(normalize_sleep(self.client.sleep(cdate), cdate))
        elif resource == "hrv":
            self.database.put_hrv(normalize_hrv(self.client.hrv(cdate), cdate))
        elif resource == "readiness":
            readiness = normalize_training_readiness(self.client.training_readiness(cdate), cdate)
            daily = self.database.get_daily(cdate)
            if daily is None:
                daily = normalize_daily_summary(self.client.daily_summary(cdate), cdate)
            self.database.put_daily(daily, readiness)
            self.database.mark_synced("readiness", cdate)
        else:
            raise ValueError(f"unsupported sync resource: {resource}")
        return True

    def ensure_training_load(self, cdate: str, *, force: bool = False) -> bool:
        if self.database.is_fresh("training_load", cdate, force=force):
            return False
        payloads: list[Any] = []
        reads: tuple[Callable[[str], Any], ...] = (
            self.client.training_status,
            self.client.max_metrics,
            self.client.endurance_score,
            self.client.hill_score,
        )
        for read in reads:
            try:
                payloads.append(read(cdate))
            except GarminOwlError:
                payloads.append(None)
        item = normalize_training_load(
            payloads[0], payloads[1], payloads[2], payloads[3], cdate
        )
        self.database.put_training_load(item)
        self.database.mark_synced("training_load", cdate)
        return True

    def ensure_activities(self, start: str, end: str, *, force: bool = False) -> bool:
        key = f"{start}:{end}"
        if self.database.is_activity_range_fresh(start, end, force=force):
            return False
        raw = self.client.activities(start, end, 100)
        for item in normalize_activities(raw, 100):
            self.database.put_activity_summary(item)
        self.database.mark_synced("activities", key)
        return True

    def sync_dates(
        self,
        requested: list[str],
        *,
        force_dates: set[str] | None = None,
        include_activities: bool = True,
    ) -> SyncReport:
        force_dates = force_dates or set()
        self.client.reset_request_counts()
        fresh = fetched = inserted = updated = 0
        for cdate in requested:
            needed = False
            for resource in ("daily", "sleep", "hrv", "readiness"):
                before = self.database.fetched_at(resource, cdate)
                try:
                    called = self.ensure_resource(resource, cdate, force=cdate in force_dates)
                except GarminOwlError:
                    # A missing optional metric does not prevent other normalized reads.
                    self.database.mark_synced(resource, cdate)
                    called = True
                if called:
                    needed = True
                    if before is None:
                        inserted += 1
                    else:
                        updated += 1
            if needed:
                fetched += 1
            else:
                fresh += 1
        if requested and include_activities:
            self.ensure_activities(requested[0], requested[-1], force=bool(force_dates))
        return SyncReport(
            requested_dates=len(requested),
            already_fresh=fresh,
            dates_fetched=fetched,
            rows_inserted=inserted,
            rows_updated=updated,
            api_calls=self.client.request_counts(),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Incrementally update the local Garmin cache.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=7)
    group.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--refresh-today", action="store_true")
    parser.add_argument("--refresh-date")
    return parser


def main() -> None:
    args = _parser().parse_args()
    today = date.today()
    if args.start:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else today
    else:
        if args.end:
            raise SystemExit("--end requires --start")
        if args.days < 1 or args.days > MAX_SYNC_DAYS:
            raise SystemExit(f"--days must be between 1 and {MAX_SYNC_DAYS}")
        end = today
        start = end - timedelta(days=args.days - 1)
    requested = dates_between(start, end)
    force_dates = {today.isoformat()} if args.refresh_today else set()
    if args.refresh_date:
        refresh = date.fromisoformat(args.refresh_date).isoformat()
        if refresh not in requested:
            raise SystemExit("--refresh-date must fall inside the requested range")
        force_dates.add(refresh)
    report = SyncEngine(GarminDataClient(), GarminDatabase()).sync_dates(
        requested, force_dates=force_dates
    )
    print(f"Requested dates: {report.requested_dates:8d}")
    print(f"Already fresh:   {report.already_fresh:8d}")
    print(f"Dates fetched:   {report.dates_fetched:8d}")
    print("\nGarmin API calls:")
    for endpoint, count in report.api_calls.items():
        print(f"  {endpoint + ':':24s}{count:4d}")
    print(f"\nRows inserted:   {report.rows_inserted:8d}")
    print(f"Rows updated:    {report.rows_updated:8d}")


if __name__ == "__main__":
    main()
