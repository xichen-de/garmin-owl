import os
import time
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def berlin_time_zone() -> Iterator[None]:
    """Render local times deterministically, in a zone with both DST transitions."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


@pytest.fixture(autouse=True)
def no_user_events(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Never read the developer's own events file during tests."""
    previous = os.environ.get("GARMIN_OWL_EVENTS")
    os.environ["GARMIN_OWL_EVENTS"] = str(tmp_path_factory.mktemp("events") / "events.toml")
    yield
    if previous is None:
        os.environ.pop("GARMIN_OWL_EVENTS", None)
    else:
        os.environ["GARMIN_OWL_EVENTS"] = previous
