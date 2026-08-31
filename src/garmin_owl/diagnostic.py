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
from .client import GarminDataClient, GarminOwlError
from .normalize import STATUS_PHRASE_KEYS
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


def _key_paths(value: Any, prefix: str = "", depth: int = 0) -> list[str]:
    """List mapping key paths only. Values are never read, so nothing private is emitted."""
    if depth > 4:
        return []
    if isinstance(value, list | tuple):
        # Only the first element is walked: siblings repeat the same shape, and the goal is
        # the set of key names, not how many records exist.
        return _key_paths(value[0], prefix, depth + 1) if value else []
    if not isinstance(value, dict):
        return []
    paths: list[str] = []
    for key in value:
        path = f"{prefix}{key}"
        paths.append(path)
        paths.extend(_key_paths(value[key], f"{path}.", depth + 1))
    return paths


def describe_training_status(raw: Any) -> dict[str, Any]:
    """Report which label-bearing keys the training-status payload carries, not their values.

    ``trainingStatus`` is an unlabeled code.  garmin-owl will not guess a code-to-label table,
    so this reports where a Garmin-supplied label could live, without emitting training data.
    """
    paths = _key_paths(raw)
    return {
        "endpoint": "get_training_status",
        "status": "ok",
        "response_type": type(raw).__name__,
        "has_training_status_key": any(p.endswith("trainingStatus") for p in paths),
        "known_label_keys_present": sorted(
            {key for key in STATUS_PHRASE_KEYS if any(p.endswith(key) for p in paths)}
        ),
        # Any other key whose name suggests wording, so an unrecognized label key can be found.
        "candidate_label_keys": sorted(
            {
                p.rsplit(".", 1)[-1]
                for p in paths
                if any(
                    hint in p.rsplit(".", 1)[-1].casefold()
                    for hint in ("phrase", "label", "key", "feedback", "description")
                )
            }
        ),
    }


def find_keys(client: GarminDataClient, cdate: str, substring: str) -> list[dict[str, Any]]:
    """Report which allow-listed Garmin reads carry keys matching ``substring``.

    Key names only -- never values -- so this answers "is this metric reachable through a read
    garmin-owl is already permitted to make?" without emitting health data.  It deliberately
    goes through GarminDataClient rather than the raw API, so the probe cannot look anywhere
    the server itself is not allowed to look.
    """
    needle = substring.casefold()
    reads: tuple[tuple[str, Callable[[], Any]], ...] = (
        ("daily_summary", lambda: client.daily_summary(cdate)),
        ("sleep", lambda: client.sleep(cdate)),
        ("hrv", lambda: client.hrv(cdate)),
        ("training_readiness", lambda: client.training_readiness(cdate)),
        ("body_battery", lambda: client.body_battery(cdate)),
        ("stress", lambda: client.stress(cdate)),
    )
    observations: list[dict[str, Any]] = []
    for name, read in reads:
        try:
            paths = _key_paths(read())
        except GarminOwlError as exc:
            observations.append(describe_failure(name, exc))
            continue
        observations.append(
            {
                "endpoint": name,
                "status": "ok",
                "key_count": len(paths),
                "matching_key_paths": sorted(p for p in paths if needle in p.casefold()),
            }
        )
    return observations


def diagnose_training_status(cdate: str) -> tuple[dict[str, Any], bool]:
    api = load_saved_client()
    try:
        return describe_training_status(api.get_training_status(cdate)), False
    except Exception as exc:  # diagnostic boundary: class only, never message/raw data
        return describe_failure("get_training_status", exc), True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run redacted, read-only diagnostics for one Garmin activity."
    )
    parser.add_argument("activity_id", type=int, nargs="?")
    parser.add_argument(
        "--training-status",
        metavar="YYYY-MM-DD",
        help="report which label keys the training-status payload carries, values excluded",
    )
    parser.add_argument(
        "--find-keys",
        nargs=2,
        metavar=("YYYY-MM-DD", "SUBSTRING"),
        help=(
            "report which allow-listed reads carry key names containing SUBSTRING "
            "(e.g. 2026-08-31 temp); key names only, never values"
        ),
    )
    args = parser.parse_args()
    if args.activity_id is None and args.training_status is None and args.find_keys is None:
        parser.error("give an activity_id, --training-status DATE, --find-keys DATE SUBSTRING")
    observations: list[dict[str, Any]] = []
    failed = False
    try:
        if args.training_status is not None:
            observation, status_failed = diagnose_training_status(args.training_status)
            observations.append(observation)
            failed = failed or status_failed
        if args.find_keys is not None:
            cdate, substring = args.find_keys
            observations.extend(find_keys(GarminDataClient(), cdate, substring))
        if args.activity_id is not None:
            activity_observations, activity_failed = diagnose_activity(args.activity_id)
            observations.extend(activity_observations)
            failed = failed or activity_failed
    except Exception as exc:
        # Authentication/setup failures receive the same no-message treatment.
        print(json.dumps({"status": "setup_failed", "exception_class": type(exc).__name__}))
        raise SystemExit(2) from None
    print(json.dumps(observations, indent=2, sort_keys=True))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
