from garmin_owl.models import DailySummary, RecoverySummary


def test_compact_omits_missing_values() -> None:
    result = DailySummary(date="2026-08-30", steps=1234).compact()
    assert result == {"date": "2026-08-30", "steps": 1234}


def test_recovery_explicitly_disclaims_derived_score() -> None:
    result = RecoverySummary(date="2026-08-30").compact()
    assert "no derived medical or recovery score" in result["note"]
    assert "score" not in result
