import json
from typing import Any

import pytest

import garmin_owl.diagnostic as diagnostic
from garmin_owl.diagnostic import describe_failure, describe_success, describe_training_status


def test_diagnostic_describes_shape_without_values() -> None:
    raw = {
        "activityId": 24_169_622_553,
        "summaryDTO": {"distance": 1500},
        "latitude": 51.0,
        "authorization": "must-not-appear",
    }
    result = describe_success("get_activity", raw)
    rendered = repr(result)
    assert result["root_has_activity_id"] is True
    assert result["summary_has_activity_id"] is False
    assert "24169622553" not in rendered
    assert "1500" not in rendered
    assert "51.0" not in rendered
    assert "must-not-appear" not in rendered


def test_diagnostic_failure_omits_exception_message() -> None:
    result = describe_failure("get_activity", RuntimeError("secret response details"))
    assert result == {
        "endpoint": "get_activity",
        "status": "failed",
        "exception_class": "RuntimeError",
    }


def test_diagnostic_identifies_individual_endpoint_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DiagnosticAPI:
        def get_activity(self, activity_id: str) -> dict[str, Any]:
            return {"activityId": int(activity_id), "summaryDTO": {}}

        def get_activity_hr_in_timezones(self, activity_id: str) -> list[Any]:
            raise LookupError("private upstream detail must not leak")

        def get_activity_power_in_timezones(self, activity_id: str) -> list[Any]:
            return []

    monkeypatch.setattr(diagnostic, "load_saved_client", DiagnosticAPI)
    observations, failed = diagnostic.diagnose_activity(123)
    assert failed is True
    assert observations == [
        {
            "endpoint": "get_activity",
            "status": "ok",
            "response_type": "dict",
            "empty": False,
            "has_summary_dto": True,
            "summary_dto_type": "dict",
            "root_has_activity_id": True,
            "summary_has_activity_id": False,
            "root_has_laps": False,
            "summary_has_laps": False,
            "root_has_activity_type": False,
            "root_has_activity_type_dto": False,
            "summary_has_activity_type": False,
            "summary_has_activity_type_dto": False,
        },
        {
            "endpoint": "get_activity_hr_in_timezones",
            "status": "failed",
            "exception_class": "LookupError",
        },
        {
            "endpoint": "get_activity_power_in_timezones",
            "status": "ok",
            "response_type": "list",
            "empty": True,
            "item_count": 0,
        },
    ]
    assert "private upstream detail" not in repr(observations)


def test_training_status_probe_reports_key_names_only() -> None:
    """The probe must locate a label key without emitting any training data."""
    raw = {
        "mostRecentTrainingStatus": {
            "latestTrainingStatusData": {
                "3442": {
                    "trainingStatus": 7,
                    "trainingStatusFeedbackPhrase": "UNPRODUCTIVE_1",
                    "weeklyTrainingLoad": 812,
                }
            }
        }
    }
    result = describe_training_status(raw)
    assert result["has_training_status_key"] is True
    assert result["known_label_keys_present"] == ["trainingStatusFeedbackPhrase"]
    assert "trainingStatusFeedbackPhrase" in result["candidate_label_keys"]
    rendered = json.dumps(result)
    for value in ("UNPRODUCTIVE_1", "812", "3442"):
        assert value not in rendered


def test_training_status_probe_surfaces_an_unrecognized_label_key() -> None:
    result = describe_training_status({"trainingStatus": 7, "statusDescription": "x"})
    assert result["known_label_keys_present"] == []
    assert "statusDescription" in result["candidate_label_keys"]
    assert "x" not in json.dumps(result)
