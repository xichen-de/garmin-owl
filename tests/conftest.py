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
