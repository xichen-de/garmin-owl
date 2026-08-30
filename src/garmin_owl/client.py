"""Narrow read-only facade around ``python-garminconnect``."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any, Protocol

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from .auth import load_saved_client


class GarminReadAPI(Protocol):
    """Only methods this project is structurally permitted to call."""

    def get_user_summary(self, cdate: str) -> dict[str, Any]: ...
    def get_sleep_data(self, cdate: str) -> dict[str, Any]: ...
    def get_hrv_data(self, cdate: str) -> dict[str, Any] | None: ...
    def get_training_readiness(self, cdate: str) -> Any: ...
    def get_body_battery(self, startdate: str, enddate: str | None = None) -> Any: ...
    def get_stress_data(self, cdate: str) -> dict[str, Any]: ...
    def get_activities(self, start: int = 0, limit: int = 20) -> Any: ...
    def get_activities_by_date(self, startdate: str, enddate: str | None = None) -> Any: ...
    def get_activity(self, activity_id: str) -> dict[str, Any]: ...
    # Upstream 0.3.11 annotates these as dict, but live endpoints return lists.
    def get_activity_hr_in_timezones(self, activity_id: str) -> dict[str, Any] | list[Any]: ...
    def get_activity_power_in_timezones(self, activity_id: str) -> dict[str, Any] | list[Any]: ...
    def get_weigh_ins(self, startdate: str, enddate: str) -> dict[str, Any]: ...
    def get_training_status(self, cdate: str) -> dict[str, Any]: ...
    def get_max_metrics(self, cdate: str) -> dict[str, Any]: ...
    def get_endurance_score(self, startdate: str, enddate: str | None = None) -> dict[str, Any]: ...
    def get_hill_score(self, startdate: str, enddate: str | None = None) -> dict[str, Any]: ...
    def get_menstrual_data_for_date(self, fordate: str) -> dict[str, Any]: ...
    def get_menstrual_calendar_data(
        self, startdate: str, enddate: str
    ) -> dict[str, Any]: ...


class GarminOwlError(RuntimeError):
    """Safe, stable error intended for an MCP client."""


class GarminOwlAuthError(GarminOwlError):
    pass


class GarminOwlRateLimitError(GarminOwlError):
    pass


class GarminOwlUnavailableError(GarminOwlError):
    pass


class GarminOwlMissingDataError(GarminOwlError):
    pass


def _safe_call[T](call: Callable[[], T]) -> T:
    """Translate upstream errors without returning sensitive response details."""
    try:
        return call()
    except GarminConnectTooManyRequestsError:
        raise GarminOwlRateLimitError(
            "Garmin rate limit reached. Wait before trying again; garmin-owl does not auto-retry."
        ) from None
    except GarminConnectAuthenticationError:
        raise GarminOwlAuthError(
            "Garmin authentication expired or was rejected. Run `garmin-owl-auth` in a terminal."
        ) from None
    except GarminConnectNotFoundError:
        raise GarminOwlMissingDataError(
            "Garmin returned no data for this request; the metric may be unsupported."
        ) from None
    except GarminConnectConnectionError:
        raise GarminOwlUnavailableError(
            "Garmin Connect is unavailable or rejected this read request. Try again later."
        ) from None
    except (KeyError, TypeError, ValueError):
        raise GarminOwlUnavailableError(
            "Garmin returned an unexpected response shape; its private API may have changed."
        ) from None


class GarminDataClient:
    """Explicit allow-list of Garmin reads; no generic request or mutation access."""

    def __init__(self, api: GarminReadAPI | None = None) -> None:
        self.__api: GarminReadAPI = api if api is not None else load_saved_client()
        self.__calls: Counter[str] = Counter()

    def _read[T](self, endpoint: str, call: Callable[[], T]) -> T:
        self.__calls[endpoint] += 1
        return _safe_call(call)

    def request_counts(self) -> dict[str, int]:
        return dict(sorted(self.__calls.items()))

    def reset_request_counts(self) -> None:
        self.__calls.clear()

    def daily_summary(self, cdate: str) -> Any:
        return self._read("daily summary", lambda: self.__api.get_user_summary(cdate))

    def sleep(self, cdate: str) -> Any:
        return self._read("sleep", lambda: self.__api.get_sleep_data(cdate))

    def hrv(self, cdate: str) -> Any:
        return self._read("HRV", lambda: self.__api.get_hrv_data(cdate))

    def training_readiness(self, cdate: str) -> Any:
        return self._read("training readiness", lambda: self.__api.get_training_readiness(cdate))

    def body_battery(self, cdate: str) -> Any:
        return self._read("body battery", lambda: self.__api.get_body_battery(cdate, cdate))

    def stress(self, cdate: str) -> Any:
        return self._read("stress", lambda: self.__api.get_stress_data(cdate))

    def activities(self, startdate: str | None, enddate: str | None, limit: int) -> Any:
        if startdate is None and enddate is None:
            return self._read("activities", lambda: self.__api.get_activities(0, limit))
        data = self._read(
            "activities",
            lambda: self.__api.get_activities_by_date(startdate or enddate or "", enddate)
        )
        return data[:limit] if isinstance(data, list) else data

    def activity(self, activity_id: int) -> tuple[Any, Any, Any]:
        activity_key = str(activity_id)
        summary = self._read("activity detail", lambda: self.__api.get_activity(activity_key))
        # These are useful but not supported for every activity/device. Absence is not fatal.
        try:
            hr_zones = self._read(
                "activity HR zones",
                lambda: self.__api.get_activity_hr_in_timezones(activity_key),
            )
        except GarminOwlError:
            hr_zones = None
        try:
            power_zones = self._read(
                "activity power zones",
                lambda: self.__api.get_activity_power_in_timezones(activity_key)
            )
        except GarminOwlError:
            power_zones = None
        return summary, hr_zones, power_zones

    def body_composition(self, startdate: str, enddate: str) -> Any:
        return self._read(
            "body composition", lambda: self.__api.get_weigh_ins(startdate, enddate)
        )

    def training_status(self, cdate: str) -> Any:
        return self._read("training status", lambda: self.__api.get_training_status(cdate))

    def max_metrics(self, cdate: str) -> Any:
        return self._read("max metrics", lambda: self.__api.get_max_metrics(cdate))

    def endurance_score(self, cdate: str) -> Any:
        return self._read("endurance score", lambda: self.__api.get_endurance_score(cdate))

    def hill_score(self, cdate: str) -> Any:
        return self._read("hill score", lambda: self.__api.get_hill_score(cdate))

    def cycle_day(self, cdate: str) -> Any:
        return self._read(
            "cycle day", lambda: self.__api.get_menstrual_data_for_date(cdate)
        )

    def cycle_calendar(self, startdate: str, enddate: str) -> Any:
        return self._read(
            "cycle calendar",
            lambda: self.__api.get_menstrual_calendar_data(startdate, enddate),
        )
