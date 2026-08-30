"""Developer-only, redacted diagnostics for the three activity reads.

This module is deliberately not registered with MCP. It reports structure and
exception classes only, never response values or exception messages.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

from .auth import load_saved_client
from .tools import validate_activity_id

_ENDPOINTS = (
    "get_activity",
    "get_activity_hr_in_timezones",
    "get_activity_power_in_timezones",
)


def describe_success(endpoint: str, value: Any) -> dict[str, Any]:
    """Return a value-free description of an upstream response container."""
    result: dict[str, Any] = {
        "endpoint": endpoint,
        "status": "ok",
        "response_type": type(value).__name__,
    }
    if isinstance(value, dict):
        nested = value.get("summaryDTO")
        result.update(
            {
                "empty": not value,
                "has_summary_dto": "summaryDTO" in value,
                "summary_dto_type": type(nested).__name__ if nested is not None else None,
                "root_has_activity_id": "activityId" in value,
                "summary_has_activity_id": isinstance(nested, dict) and "activityId" in nested,
                "root_has_laps": "lapDTOs" in value,
                "summary_has_laps": isinstance(nested, dict) and "lapDTOs" in nested,
                "root_has_activity_type": "activityType" in value,
                "root_has_activity_type_dto": "activityTypeDTO" in value,
                "summary_has_activity_type": isinstance(nested, dict) and "activityType" in nested,
                "summary_has_activity_type_dto": isinstance(nested, dict)
                and "activityTypeDTO" in nested,
            }
        )
    elif isinstance(value, list | tuple):
        result.update({"empty": not value, "item_count": len(value)})
    return result


def describe_failure(endpoint: str, exc: Exception) -> dict[str, str]:
    """Describe a failure without rendering its potentially sensitive message."""
    return {
        "endpoint": endpoint,
        "status": "failed",
        "exception_class": type(exc).__name__,
    }


def diagnose_activity(activity_id: int) -> tuple[list[dict[str, Any]], bool]:
    """Run the exact three reads independently and return redacted observations."""
    validate_activity_id(activity_id)
    api = load_saved_client()
    activity_key = str(activity_id)
    observations: list[dict[str, Any]] = []
    failed = False

    for endpoint in _ENDPOINTS:
        method: Callable[[str], Any] = getattr(api, endpoint)
        try:
            observations.append(describe_success(endpoint, method(activity_key)))
        except Exception as exc:  # diagnostic boundary: class only, never message/raw data
            observations.append(describe_failure(endpoint, exc))
            failed = True
    return observations, failed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run redacted, read-only diagnostics for one Garmin activity."
    )
    parser.add_argument("activity_id", type=int)
    args = parser.parse_args()
    try:
        observations, failed = diagnose_activity(args.activity_id)
    except Exception as exc:
        # Authentication/setup failures receive the same no-message treatment.
        print(json.dumps({"status": "setup_failed", "exception_class": type(exc).__name__}))
        raise SystemExit(2) from None
    print(json.dumps(observations, indent=2, sort_keys=True))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
