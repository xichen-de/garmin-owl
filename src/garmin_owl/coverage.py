"""Discover where the user's Garmin history starts and ends, per metric family.

Garmin has no "first record" endpoint, so each family is bounded with the fewest range reads
that can find it: resting HR backwards in 366-day windows from today, then HRV, sleep and VO2
max scanned from that first wellness date. The answer is cached for a day.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Any

from .client import (
    GarminDataClient,
    GarminOwlAuthError,
    GarminOwlError,
    GarminOwlRateLimitError,
)
from .database import GarminDatabase
from .models import AvailabilityNotice, CoverageFamily, DataCoverage, DeviceInfo
from .normalize import (
    normalize_activities,
    normalize_hrv_range,
    normalize_resting_hr_range,
    normalize_sleep_range,
)
from .notices import RATE_LIMITED, RETRIEVAL_FAILED
from .series import HRV_RANGE_DAYS, RESTING_HR_RANGE_DAYS, SLEEP_RANGE_DAYS

COVERAGE_KEY = "data_coverage"
COVERAGE_MAX_AGE = timedelta(hours=24)
MAX_HISTORY_WINDOWS = 10  # ten years of 366-day windows
EMPTY_WINDOWS_TO_STOP = 2  # about two years without data ends a backward search
MAX_SLEEP_WINDOWS = 13  # about a year of 28-day sleep windows from each end
VO2_RANGE_DAYS = 366
ACTIVITY_PAGE = 20
MAX_ACTIVITY_PAGES = 5


def vo2_dates(raw: Any) -> list[str]:
    """Calendar dates on which Garmin's max-metrics history carries a VO2 max value."""
    dates = []
    for item in raw if isinstance(raw, list) else []:
        for sport in ("generic", "cycling"):
            entry = item.get(sport) if isinstance(item, dict) else None
            if (
                isinstance(entry, dict)
                and entry.get("calendarDate")
                and (
                    entry.get("vo2MaxPreciseValue") is not None
                    or entry.get("vo2MaxValue") is not None
                )
            ):
                dates.append(str(entry["calendarDate"])[:10])
    return sorted(set(dates))


def regular_from(days: list[str], *, window: int = 7, minimum: int = 4) -> str | None:
    """First day that opens a ``window``-day stretch holding at least ``minimum`` data days."""
    parsed = [date.fromisoformat(day) for day in days]
    for index, first in enumerate(parsed):
        end = first + timedelta(days=window - 1)
        if sum(1 for day in parsed[index : index + window] if day <= end) >= minimum:
            return first.isoformat()
    return None


def _windows(start: date, end: date, size: int) -> Iterable[tuple[str, str]]:
    while start <= end:
        chunk_end = min(start + timedelta(days=size - 1), end)
        yield start.isoformat(), chunk_end.isoformat()
        start = chunk_end + timedelta(days=1)


class CoverageReader:
    def __init__(self, client: GarminDataClient, database: GarminDatabase | None) -> None:
        self.client = client
        self.database = database
        self.notices: list[AvailabilityNotice] = []

    def _guard[T](self, family: str, read: Callable[[], T]) -> T | None:
        try:
            return read()
        except GarminOwlAuthError:
            raise
        except GarminOwlError as exc:
            status = RATE_LIMITED if isinstance(exc, GarminOwlRateLimitError) else RETRIEVAL_FAILED
            self.notices.append(
                AvailabilityNotice(
                    field=family,
                    status=status,
                    message=f"Could not determine {family} coverage: {exc} The dates are "
                    "unknown, not absent.",
                )
            )
            return None

    def _backward(
        self, family: str, today: date, size: int, read: Callable[[str, str], list[str]]
    ) -> list[str]:
        """Every day ``read`` reports, scanning back from today in ``size``-day windows until
        ``EMPTY_WINDOWS_TO_STOP`` consecutive windows are empty after data was found, so a single
        year without, say, outdoor VO2 estimates does not hide the years before it.

        If the search limit is reached while windows still hold data, earlier days may exist:
        that is disclosed, which also keeps the answer out of the 24-hour cache.
        """
        found: list[str] = []
        empty = 0
        end = today
        for _ in range(MAX_HISTORY_WINDOWS):
            start = end - timedelta(days=size - 1)
            days = sorted(read(start.isoformat(), end.isoformat()))
            empty = empty + 1 if not days else 0
            if found and empty >= EMPTY_WINDOWS_TO_STOP:
                return found
            found = days + found
            end = start - timedelta(days=1)
        if found:
            self.notices.append(
                AvailabilityNotice(
                    field=family,
                    status="search_limit_reached",
                    message=f"{family} data reaches the search limit of {MAX_HISTORY_WINDOWS} "
                    f"windows back from today; its first_date is unknown and may be earlier.",
                )
            )
        return found

    def _rhr_days(self, start: str, end: str) -> list[str]:
        return list(normalize_resting_hr_range(self.client.resting_hr_range(start, end)))

    def _scan(
        self,
        windows: Iterable[tuple[str, str]],
        read: Callable[[str, str], list[str]],
        limit: int,
        *,
        last: bool = False,
    ) -> str | None:
        """First (or last) date ``read`` reports, trying at most ``limit`` windows."""
        for index, (start, end) in enumerate(windows):
            if index >= limit:
                break
            days = read(start, end)
            if days:
                return days[-1] if last else days[0]
        return None

    def _hrv_days(self, start: str, end: str) -> list[str]:
        items = normalize_hrv_range(self.client.hrv_range(start, end))
        return sorted(
            cdate
            for cdate, item in items.items()
            if item.nightly_average_ms is not None or item.last_night_average_ms is not None
        )

    def _sleep_days(self, start: str, end: str) -> list[str]:
        items = normalize_sleep_range(self.client.sleep_range(start, end))
        return sorted(
            cdate
            for cdate, item in items.items()
            if item.sleep_score is not None or item.total_sleep_seconds is not None
        )

    def _oldest_activities(self, count: int) -> Any:
        """The last listable page. Garmin's count can exceed what its list returns (verified
        live: 478 counted, 476 listed), so step back from the end until a page has entries."""
        offset = max(count - ACTIVITY_PAGE, 0)
        for _ in range(MAX_ACTIVITY_PAGES):
            page = self.client.activity_page(offset, ACTIVITY_PAGE)
            if page or offset == 0:
                return page
            offset = max(offset - ACTIVITY_PAGE, 0)
        return []

    def _vo2_window(self, start: str, end: str) -> list[str]:
        return vo2_dates(self.client.max_metrics_range(start, end))

    def read(self) -> DataCoverage:
        today = date.today()
        families: list[CoverageFamily] = []
        wellness = (
            self._guard(
                "daily_summary",
                lambda: self._backward(
                    "daily_summary", today, RESTING_HR_RANGE_DAYS, self._rhr_days
                ),
            )
            or []
        )
        if wellness:
            families.append(
                CoverageFamily(
                    family="daily_summary",
                    first_date=wellness[0],
                    last_date=wellness[-1],
                    days_with_data=len(wellness),
                    regular_from=regular_from(wellness),
                    method="Days with a Garmin resting heart rate, read in 366-day windows "
                    "back from today until two consecutive windows were empty.",
                )
            )
        # HRV and VO2 max have cheap 366-day range reads, so each is searched on its own rather
        # than from the wellness span: estimates before regular wear, or without any resting-HR
        # history, are still found.
        hrv = (
            self._guard("hrv", lambda: self._backward("hrv", today, HRV_RANGE_DAYS, self._hrv_days))
            or []
        )
        if hrv:
            families.append(
                CoverageFamily(
                    family="hrv",
                    first_date=hrv[0],
                    last_date=hrv[-1],
                    days_with_data=len(hrv),
                    regular_from=regular_from(hrv),
                    method="Nights with a Garmin HRV average, read in 366-day windows back from "
                    "today until two consecutive windows were empty.",
                )
            )
        vo2 = (
            self._guard(
                "vo2_max",
                lambda: self._backward("vo2_max", today, VO2_RANGE_DAYS, self._vo2_window),
            )
            or []
        )
        if vo2:
            families.append(
                CoverageFamily(
                    family="vo2_max",
                    first_date=vo2[0],
                    last_date=vo2[-1],
                    days_with_data=len(vo2),
                    method="Days on which Garmin's max-metrics history has a VO2 max, read in "
                    "366-day windows back from today until two consecutive windows were empty.",
                )
            )
        if wellness:
            # Sleep reads cover only 28 days, so a ten-year backward search would cost about
            # 130 requests; it is bounded from where regular wear began instead, and says so.
            first = date.fromisoformat(regular_from(wellness) or wellness[0])
            sleep_windows = list(_windows(first, today, SLEEP_RANGE_DAYS))
            sleep_first = self._guard(
                "sleep", lambda: self._scan(sleep_windows, self._sleep_days, MAX_SLEEP_WINDOWS)
            )
            sleep_last = self._guard(
                "sleep",
                lambda: self._scan(
                    reversed(sleep_windows), self._sleep_days, MAX_SLEEP_WINDOWS, last=True
                ),
            )
            if sleep_first or sleep_last:
                families.append(
                    CoverageFamily(
                        family="sleep",
                        first_date=sleep_first,
                        last_date=sleep_last,
                        method="Nights with a Garmin sleep score or duration, searched up to a "
                        "year inward from each end of the span starting at the resting-HR "
                        "regular_from; isolated nights before that are not searched.",
                    )
                )
        elif hrv:
            self.notices.append(
                AvailabilityNotice(
                    field="sleep",
                    status="not_searched",
                    message="Sleep coverage is searched from the resting-HR history, which "
                    "Garmin did not return, so sleep dates are unknown, not absent.",
                )
            )
        if self.database is not None:
            ready_first, ready_last = self.database.cached_readiness_dates()
            if ready_first:
                families.append(
                    CoverageFamily(
                        family="training_readiness",
                        first_date=ready_first,
                        last_date=ready_last,
                        method="Locally cached days only: Garmin has no range read for "
                        "readiness, so earlier uncached days may exist.",
                    )
                )
        count = self._guard("activities", self.client.activity_count)
        if count:
            newest = self._guard("activities", lambda: self.client.activity_page(0, 1))
            oldest = self._guard("activities", lambda: self._oldest_activities(count))
            dates = [
                item.start_time[:10]
                for page in (oldest, newest)
                for item in normalize_activities(page)
                if item.start_time
            ]
            if dates:
                families.append(
                    CoverageFamily(
                        family="activities",
                        first_date=min(dates),
                        last_date=max(dates),
                        method="Oldest and newest activity in Garmin's activity list.",
                    )
                )
        devices = self._guard("devices", self.client.devices) or []
        return DataCoverage(
            families=families,
            activity_count=count,
            devices=[_device(item) for item in devices if isinstance(item, dict)],
            checked_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            availability=self.notices,
        )


def _device(item: dict[str, Any]) -> DeviceInfo:
    """Model name, status and registration only; serial numbers and IDs are never returned."""
    registered = item.get("registeredDate")
    return DeviceInfo(
        product_name=item.get("productDisplayName") or None,
        status=item.get("deviceStatus") or None,
        registered_date=(
            datetime.fromtimestamp(registered / 1000).astimezone().date().isoformat()
            if isinstance(registered, int | float) and not isinstance(registered, bool)
            else None
        ),
        # Garmin sets ``primary`` on every paired device (verified live: watch and chest strap);
        # only this flag marks the one device that counts as the activity tracker.
        primary=(
            tracker
            if isinstance(tracker := item.get("primaryActivityTrackerIndicator"), bool)
            else None
        ),
    )


def data_coverage(
    client: GarminDataClient, database: GarminDatabase | None, *, refresh: bool = False
) -> dict[str, Any]:
    if database is not None and not refresh:
        cached = database.get_metadata(COVERAGE_KEY, max_age=COVERAGE_MAX_AGE)
        if cached is not None:
            return dict(json.loads(cached))
    result = CoverageReader(client, database).read().compact()
    # A partial answer is not cached, so the next call retries what failed.
    if database is not None and "availability" not in result:
        database.put_metadata(COVERAGE_KEY, json.dumps(result))
    return result
