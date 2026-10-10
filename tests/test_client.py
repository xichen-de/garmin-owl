"""Upstream failures map to distinct, accurate, data-free messages."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import requests
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from garmin_owl.client import (
    BODY_BATTERY_RANGE_DAYS,
    GarminDataClient,
    GarminOwlAuthError,
    GarminOwlError,
    GarminOwlMissingDataError,
    GarminOwlNetworkError,
    GarminOwlRateLimitError,
    GarminOwlRequestError,
    GarminOwlResponseError,
    GarminOwlServerError,
)


def _http_error(status: int) -> GarminConnectConnectionError:
    """Mirror upstream: the status is in a fixed message prefix and on ``.response``."""
    response = requests.Response()
    response.status_code = status
    error = GarminConnectConnectionError(
        f"API client error ({status}): body with SECRET-PAYLOAD 71 bpm"
    )
    error.response = response
    return error


def _network_error() -> GarminConnectConnectionError:
    try:
        raise requests.ConnectionError("Failed to resolve connectapi.garmin.com")
    except requests.ConnectionError as cause:
        try:
            raise GarminConnectConnectionError("Connection error: SECRET-PAYLOAD") from cause
        except GarminConnectConnectionError as error:
            return error


class _Failing:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def get_sleep_data(self, cdate: str) -> Any:
        raise self.error


@pytest.mark.parametrize(
    ("error", "expected", "fragment", "try_later"),
    [
        (_http_error(400), GarminOwlRequestError, "invalid or too large (HTTP 400)", False),
        (_http_error(413), GarminOwlRequestError, "HTTP 413", False),
        (_http_error(503), GarminOwlServerError, "server error (HTTP 503)", True),
        (_network_error(), GarminOwlNetworkError, "Could not reach Garmin Connect", True),
        (
            GarminConnectTooManyRequestsError("Rate limit exceeded"),
            GarminOwlRateLimitError,
            "HTTP 429",
            False,
        ),
        (
            GarminConnectAuthenticationError("Authentication failed"),
            GarminOwlAuthError,
            "garmin-owl-auth",
            False,
        ),
        (GarminConnectNotFoundError("404"), GarminOwlMissingDataError, "no data", False),
        (GarminConnectConnectionError("No data received"), GarminOwlResponseError, "", False),
        (KeyError("payload"), GarminOwlResponseError, "unexpected response shape", False),
    ],
)
def test_upstream_failures_map_to_distinct_messages(
    error: BaseException, expected: type[GarminOwlError], fragment: str, try_later: bool
) -> None:
    client = GarminDataClient(_Failing(error))  # type: ignore[arg-type]
    with pytest.raises(GarminOwlError) as caught:
        client.sleep("2026-10-10")
    assert type(caught.value) is expected
    message = str(caught.value)
    assert fragment in message
    # Only transient failures (network, Garmin 5xx) suggest that the same request may work later.
    assert ("try again later" in message.lower()) is try_later
    assert "71 bpm" not in message and "SECRET-PAYLOAD" not in message
    assert caught.value.__cause__ is None


class _BodyBatteryLimit:
    """Behaves like Garmin's daily report: HTTP 400 for ranges longer than 31 days."""

    def __init__(self) -> None:
        self.ranges: list[tuple[str, str]] = []

    def get_body_battery(self, startdate: str, enddate: str | None = None) -> list[dict[str, Any]]:
        end = enddate or startdate
        days = (date.fromisoformat(end) - date.fromisoformat(startdate)).days + 1
        if days > BODY_BATTERY_RANGE_DAYS:
            raise _http_error(400)
        self.ranges.append((startdate, end))
        first = date.fromisoformat(startdate).toordinal()
        return [
            {"date": date.fromordinal(first + offset).isoformat(), "charged": 40}
            for offset in range(days)
        ]


def test_body_battery_range_is_split_into_chunks_garmin_accepts() -> None:
    api = _BodyBatteryLimit()
    client = GarminDataClient(api)  # type: ignore[arg-type]
    rows = client.body_battery_range("2026-07-13", "2026-10-10")
    assert len(rows) == 90
    assert [row["date"] for row in rows] == sorted({row["date"] for row in rows})
    assert api.ranges == [
        ("2026-07-13", "2026-08-12"),
        ("2026-08-13", "2026-09-12"),
        ("2026-09-13", "2026-10-10"),
    ]
    assert client.request_counts() == {"body battery range": 3}


def test_short_body_battery_range_is_one_read() -> None:
    api = _BodyBatteryLimit()
    GarminDataClient(api).body_battery_range("2026-10-04", "2026-10-10")  # type: ignore[arg-type]
    assert api.ranges == [("2026-10-04", "2026-10-10")]
