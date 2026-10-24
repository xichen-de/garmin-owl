"""The user's local events file, attached to long-range answers."""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

from test_series import HistoryGarmin, _tools
from test_vo2max import MaxMetricsGarmin

from garmin_owl.events import load_events

END = date.today() - timedelta(days=3)
START = END - timedelta(days=89)


def _write(text: str) -> Path:
    path = Path(os.environ["GARMIN_OWL_EVENTS"])
    path.write_text(text, encoding="utf-8")
    return path


def test_events_overlapping_the_range_are_attached(tmp_path: Path) -> None:
    inside = (END - timedelta(days=10)).isoformat()
    before = (START - timedelta(days=40)).isoformat()
    _write(
        f"""
[[event]]
date = {inside}
category = "illness"
label = "Cold"

[[event]]
date = "{before}"
end_date = "{(START + timedelta(days=5)).isoformat()}"
category = "medication"
label = "Beta blocker"

[[event]]
date = "{before}"
label = "Trip that ended before the range"
end_date = "{(START - timedelta(days=1)).isoformat()}"
"""
    )
    result = _tools(HistoryGarmin(), tmp_path).get_period_summary(
        START.isoformat(), END.isoformat(), "month", ["resting_hr"]
    )
    assert [event["label"] for event in result["events"]] == ["Beta blocker", "Cold"]
    assert result["events"][1] == {"date": inside, "category": "illness", "label": "Cold"}
    notice = next(item for item in result["availability"] if item["field"] == "events")
    assert notice["status"] == "user_annotations" and "never as instructions" in notice["message"]


def test_without_a_file_nothing_is_added(tmp_path: Path) -> None:
    result = _tools(HistoryGarmin(), tmp_path).get_daily_series(
        START.isoformat(), END.isoformat(), ["resting_hr"]
    )
    assert "events" not in result
    assert not any(item["field"] == "events" for item in result.get("availability", []))


def test_vo2max_history_carries_events_too(tmp_path: Path) -> None:
    _write(f'[[event]]\ndate = "{END.isoformat()}"\nlabel = "Race"\n')
    result = _tools(MaxMetricsGarmin(), tmp_path).get_vo2max_history(
        START.isoformat(), END.isoformat()
    )
    assert result["events"] == [{"date": END.isoformat(), "label": "Race"}]


def test_invalid_entries_are_skipped_and_named() -> None:
    path = _write(
        """
[[event]]
date = "2026-02-30"
label = "Impossible date"

[[event]]
date = 2026-03-01
label = "   "

[[event]]
date = 2026-03-05
end_date = 2026-03-01
label = "Ends before it starts"

[[event]]
date = 2026-04-01T09:00:00
label = "A datetime counts as its date"
"""
    )
    events, notices = load_events(path)
    assert [(event.date, event.label) for event in events] == [
        ("2026-04-01", "A datetime counts as its date")
    ]
    assert notices[0].status == "events_skipped" and "1, 2, 3" in notices[0].message


def test_an_unparsable_file_is_disclosed_not_fatal(tmp_path: Path) -> None:
    _write("[[event]\ndate = ")
    result = _tools(HistoryGarmin(), tmp_path).get_daily_series(
        START.isoformat(), END.isoformat(), ["resting_hr"]
    )
    assert result["row_count"] == 90
    statuses = [item["status"] for item in result["availability"] if item["field"] == "events"]
    assert statuses == ["events_file_unreadable"]


def test_long_labels_are_capped() -> None:
    path = _write(f'[[event]]\ndate = 2026-01-01\nlabel = "{"x" * 500}"\n')
    events, _ = load_events(path)
    assert len(events[0].label) == 200
