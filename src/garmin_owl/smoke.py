"""Local, value-free end-to-end smoke check."""

from __future__ import annotations

from datetime import date

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from .client import GarminDataClient, GarminOwlError
from .database import GarminDatabase
from .normalize import (
    normalize_activities,
    normalize_activity_detail,
    normalize_daily_summary,
    normalize_hrv,
    normalize_sleep,
    normalize_training_load,
    normalize_training_readiness,
)


def main() -> None:
    try:
        client = GarminDataClient()
    except (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    ) as exc:
        print(f"authentication: unavailable ({exc.__class__.__name__})")
        print("Garmin API calls: 0")
        return
    database = GarminDatabase()
    cdate = date.today().isoformat()
    checks: list[tuple[str, bool]] = [("authentication", True)]

    def check(name: str, call: object) -> None:
        checks.append((name, call is not None))

    try:
        check("database", database.info())
        check("daily summary", normalize_daily_summary(client.daily_summary(cdate), cdate))
        check("sleep", normalize_sleep(client.sleep(cdate), cdate))
        check("HRV", normalize_hrv(client.hrv(cdate), cdate))
        check(
            "training readiness",
            normalize_training_readiness(client.training_readiness(cdate), cdate),
        )
        activities = normalize_activities(client.activities(None, None, 1), 1)
        check("latest activity", activities)
        if activities:
            summary, hr, power = client.activity(activities[0].activity_id)
            check("activity detail", normalize_activity_detail(summary, None, hr, power))
        load_payloads = []
        for read in (
            client.training_status,
            client.max_metrics,
            client.endurance_score,
            client.hill_score,
        ):
            try:
                load_payloads.append(read(cdate))
            except GarminOwlError:
                load_payloads.append(None)
        check(
            "training status/load",
            normalize_training_load(
                load_payloads[0],
                load_payloads[1],
                load_payloads[2],
                load_payloads[3],
                cdate,
            ),
        )
    except GarminOwlError as exc:
        print(f"Smoke check stopped safely: {exc.__class__.__name__}")

    for name, passed in checks:
        print(f"{name}: {'ok' if passed else 'unavailable'}")
    print(f"Garmin API calls: {sum(client.request_counts().values())}")


if __name__ == "__main__":
    main()
