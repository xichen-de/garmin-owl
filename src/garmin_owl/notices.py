"""Shared availability notices.

Every notice distinguishes *why* a field is absent.  The same builder is used when a value is
normalized from Garmin and when it is restored from the local cache, so a cache hit can never
present a narrower or less-qualified answer than the original read.
"""

from __future__ import annotations

from .models import AvailabilityNotice

# Distinguishes the conditions the review contract requires us to keep apart.
MISSING_OR_UNSUPPORTED = "missing_or_unsupported"
RETRIEVAL_FAILED = "retrieval_failed"
RATE_LIMITED = "retrieval_failed_rate_limited"
DATE_MISMATCH = "date_mismatch"
DERIVED = "garmin_owl_derived"

TRAINING_LOAD_SOURCES = ("training_status", "max_metrics", "endurance_score", "hill_score")

_SOURCE_LABELS = {
    "training_status": "training status",
    "max_metrics": "VO2 max metrics",
    "endurance_score": "endurance score",
    "hill_score": "hill score",
}


def unavailable_source_notice(source: str) -> AvailabilityNotice:
    label = _SOURCE_LABELS.get(source, source.replace("_", " "))
    return AvailabilityNotice(
        field=source,
        status=MISSING_OR_UNSUPPORTED,
        message=(
            f"Garmin returned no {label} for this date; fields sourced from it are absent "
            "rather than zero."
        ),
    )


def unlabeled_status_notice(code: int) -> AvailabilityNotice:
    return AvailabilityNotice(
        field="training_status",
        status="code_without_label",
        message=(
            f"Garmin returned training-status code {code} with no accompanying wording. "
            "garmin-owl does not guess what the code means; the number is reported as "
            "training_status_code so it is not mistaken for a Garmin status name."
        ),
    )


def body_battery_notice(status: str, cdate: str) -> AvailabilityNotice:
    messages = {
        MISSING_OR_UNSUPPORTED: (
            f"Garmin returned no Body Battery record for {cdate}."
        ),
        DATE_MISMATCH: (
            f"Garmin returned Body Battery records, but none for {cdate}; values from other "
            "dates were discarded rather than reported as this date's."
        ),
    }
    return AvailabilityNotice(
        field="body_battery",
        status=status,
        message=messages.get(status, f"Body Battery for {cdate} is {status}."),
    )


def cycle_notices(status: str, cdate: str) -> list[AvailabilityNotice]:
    if status == "available":
        return []
    return [
        AvailabilityNotice(
            field="cycle",
            status=status,
            message=f"Garmin did not provide cycle tracking data for {cdate}.",
        )
    ]


def derived_notice(field: str, formula: str) -> AvailabilityNotice:
    return AvailabilityNotice(
        field=field,
        status=DERIVED,
        message=f"Calculated by garmin-owl, not returned by Garmin: {formula}",
    )


def partial_coverage_notice(
    field: str, covered: int, total: int, detail: str
) -> AvailabilityNotice:
    return AvailabilityNotice(
        field=field,
        status="partial_coverage",
        message=f"{detail} Covered {covered} of {total} Garmin records.",
    )
