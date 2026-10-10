from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from garmin_owl.server import mcp


def test_server_registers_only_the_intended_read_tools() -> None:
    async def names() -> set[str]:
        # In-memory MCP still performs protocol initialization and schema exchange.
        async with Client(mcp) as client:
            result = await client.list_tools()
            return {tool.name for tool in result.tools}

    assert anyio.run(names) == {
        "get_daily_summary",
        "get_sleep",
        "get_hrv",
        "get_recovery",
        "get_training_readiness",
        "get_body_battery",
        "get_stress",
        "get_activities",
        "get_activity",
        "get_body_composition",
        "get_training_context",
        "get_recovery_trend",
        "get_training_week",
        "get_training_load",
        "get_training_zones",
        "get_running_tolerance",
        "get_recent_activities",
        "compare_activities",
        "get_cycle",
    }


def test_exported_tool_metadata_is_complete_without_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import garmin_owl.server as server

    def unexpected_authentication() -> None:
        raise AssertionError("Listing tools must not authenticate or access Garmin")

    monkeypatch.setattr(server, "get_tools", unexpected_authentication)

    async def inspect() -> None:
        async with Client(mcp) as client:
            result = await client.list_tools()
            by_name = {tool.name: tool for tool in result.tools}
            for tool in result.tools:
                assert tool.annotations is not None, tool.name
                assert tool.annotations.read_only_hint is True, tool.name
                assert tool.annotations.destructive_hint is False, tool.name
                assert tool.annotations.open_world_hint is True, tool.name
                assert tool.description and "garmin-owl-auth" in tool.description, tool.name
                for name, schema in tool.input_schema.get("properties", {}).items():
                    assert schema.get("description"), (tool.name, name)

            readiness = by_name["get_training_readiness"]
            date = readiness.input_schema["properties"]["date"]
            assert "YYYY-MM-DD" in date["description"]
            assert "timezone" in date["description"]
            assert date["default"] is None
            assert "date" not in readiness.input_schema.get("required", [])
            assert "get_recovery" in (readiness.description or "")

            activity = by_name["get_activity"].input_schema
            assert activity["required"] == ["activity_id"]
            assert activity["properties"]["refresh"]["default"] is False
            activities = by_name["get_activities"].input_schema["properties"]
            assert activities["limit"]["default"] == 20
            assert "14 days" in activities["start_date"]["description"]
            assert "29 days" in activities["start_date"]["description"]

    anyio.run(inspect)


def test_typed_outputs_preserve_service_payloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import json

    from jsonschema import Draft202012Validator
    from test_tools import DATE, CycleGarmin, _client

    import garmin_owl.server as server
    from garmin_owl.database import GarminDatabase
    from garmin_owl.tools import GarminTools

    fake = CycleGarmin()
    for method in (
        "get_training_status",
        "get_max_metrics",
        "get_endurance_score",
        "get_hill_score",
    ):
        monkeypatch.setattr(fake, method, lambda date: {}, raising=False)
    service = GarminTools(_client(fake), GarminDatabase(tmp_path / "schema.sqlite"))
    monkeypatch.setattr(server, "get_tools", lambda: service)

    async def inspect() -> None:
        async with Client(mcp) as client:
            definitions = await client.list_tools()
            for tool in definitions.tools:
                properties = tool.input_schema.get("properties", {})
                arguments: dict[str, Any] = {
                    key: DATE for key in ("date", "start_date", "end_date") if key in properties
                }
                if "activity_id" in properties:
                    arguments["activity_id"] = 123
                if "activity_ids" in properties:
                    arguments["activity_ids"] = [123, 456]
                expected = getattr(service, tool.name)(**arguments)
                # Replay the service result so cache warming cannot change the comparison.
                with monkeypatch.context() as patch:
                    patch.setattr(
                        service, tool.name, lambda *args, payload=expected, **kwargs: payload
                    )
                    result = await client.call_tool(tool.name, arguments)
                assert not result.is_error, (tool.name, result)
                assert tool.output_schema is not None, tool.name
                Draft202012Validator.check_schema(tool.output_schema)
                Draft202012Validator(tool.output_schema).validate(result.structured_content)
                assert result.structured_content == (
                    {"result": expected} if isinstance(expected, list) else expected
                ), tool.name
                assert [
                    json.loads(block.text) for block in result.content if block.type == "text"
                ] == (expected if isinstance(expected, list) else [expected]), tool.name
                # List schemas describe their elements, not merely an untyped result array.
                schema = tool.output_schema
                if isinstance(expected, list):
                    assert "$ref" in schema["properties"]["result"]["items"], tool.name
                else:
                    assert schema.get("properties"), tool.name

    anyio.run(inspect)


def test_readiness_schema_allows_absent_score_and_rejects_wrong_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jsonschema import Draft202012Validator, ValidationError

    import garmin_owl.server as server

    class MissingReadiness:
        def get_training_readiness(self, date: str | None) -> dict[str, str]:
            return {"date": "2026-10-10"}

    monkeypatch.setattr(server, "get_tools", MissingReadiness)

    async def inspect() -> None:
        async with Client(mcp) as client:
            definitions = await client.list_tools()
            tool = next(t for t in definitions.tools if t.name == "get_training_readiness")
            assert tool.output_schema is not None
            validator = Draft202012Validator(tool.output_schema)
            result = await client.call_tool(tool.name, {})
            assert not result.is_error
            assert result.structured_content == {"date": "2026-10-10"}
            validator.validate(result.structured_content)
            with pytest.raises(ValidationError):
                validator.validate({"date": "2026-10-10", "score": "unknown"})
            with pytest.raises(ValidationError):
                validator.validate({"score": 75})

    anyio.run(inspect)


@pytest.mark.parametrize("kind", ["input", "cache", "auth", "unexpected"])
def test_errors_expose_only_safe_messages(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    import garmin_owl.server as server
    from garmin_owl.client import GarminOwlAuthError
    from garmin_owl.database import GarminOwlCacheError
    from garmin_owl.tools import GarminOwlInputError

    failures = {
        "input": GarminOwlInputError("date must use exact YYYY-MM-DD format"),
        "cache": GarminOwlCacheError("Cache ownership is unknown. Use a new GARMIN_OWL_DB."),
        "auth": GarminOwlAuthError("Run garmin-owl-auth in a terminal."),
        "unexpected": ValueError("private upstream payload must not leak"),
    }

    def fail() -> None:
        raise failures[kind]

    monkeypatch.setattr(server, "get_tools", fail)

    async def check() -> None:
        async with Client(mcp) as client:
            result = await client.call_tool("get_sleep", {})
            assert result.is_error
            text = " ".join(block.text for block in result.content if block.type == "text")
            if kind == "unexpected":
                assert "private upstream" not in text
                assert "unexpected internal error (ValueError)" in text
                assert "get_sleep" in text
            else:
                assert str(failures[kind]) in text

    anyio.run(check)


def test_invalid_trend_window_rejected_before_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    import garmin_owl.server as server

    def fail() -> None:
        raise AssertionError("Invalid input must not initialize Garmin")

    monkeypatch.setattr(server, "get_tools", fail)

    async def check() -> None:
        async with Client(mcp) as client:
            for days in (0, 29, 90, 365):
                result = await client.call_tool("get_recovery_trend", {"days": days})
                assert result.is_error
                text = " ".join(block.text for block in result.content if block.type == "text")
                assert "days" in text
                assert "28" in text if days > 28 else "1" in text

    anyio.run(check)


def test_cache_errors_name_the_cache_without_leaking_sqlite_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    import garmin_owl.server as server

    def fail() -> None:
        raise sqlite3.OperationalError("near 'resting_hr_bpm=52': syntax error")

    monkeypatch.setattr(server, "get_tools", fail)

    async def check() -> None:
        async with Client(mcp) as client:
            result = await client.call_tool("get_recovery", {})
            assert result.is_error
            text = " ".join(block.text for block in result.content if block.type == "text")
            assert "local garmin-owl cache" in text and "OperationalError" in text
            assert "garmin-owl-cache-clear" in text
            assert "52" not in text

    anyio.run(check)


def test_cache_from_before_account_binding_no_longer_breaks_every_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: a 0.2.6 cache (schema 6, rows, no owner) failed every call after updating."""
    import sqlite3

    import garmin_owl.server as server
    from garmin_owl import client as client_module
    from garmin_owl.database import GarminDatabase
    from garmin_owl.models import DailySummary

    path = tmp_path / "garmin.sqlite"
    legacy = GarminDatabase(path)
    legacy.put_daily(DailySummary(date="2026-10-01", steps=1))
    legacy.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE cache_owner")
        connection.execute("PRAGMA user_version = 6")

    class Account:
        display_name: str | None = None

        def connectapi(self, path: str, *, timeout: int) -> dict[str, str]:
            return {"displayName": "account-a"}

        def get_sleep_data(self, cdate: str) -> dict[str, Any]:
            return {
                "dailySleepDTO": {
                    "sleepTimeSeconds": 28000,
                    "sleepStartTimestampGMT": 1791580696000,
                    "sleepStartTimestampLocal": 1791587896000,
                }
            }

    monkeypatch.setenv("GARMIN_OWL_DB", str(path))
    monkeypatch.setattr(client_module, "load_saved_client", Account)
    monkeypatch.setattr(server, "_tools", None)

    async def check() -> None:
        async with Client(mcp) as client:
            result = await client.call_tool("get_sleep", {"date": "2026-10-10"})
            assert not result.is_error, result
            assert result.structured_content is not None
            assert result.structured_content["sleep_start"] == "2026-10-09T23:18:16+02:00"

    try:
        anyio.run(check)
    finally:
        if server._tools is not None and server._tools.database is not None:
            server._tools.database.close()
        server._tools = None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM daily_metrics").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM cache_owner").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_activities", {"limit": 0}),
        ("get_activities", {"limit": 101}),
        ("get_recent_activities", {"days": 91}),
        ("get_running_tolerance", {"days": 0}),
        ("get_activity", {"activity_id": 0}),
        ("compare_activities", {"activity_ids": [1]}),
        ("compare_activities", {"activity_ids": list(range(1, 12))}),
    ],
)
def test_schema_bounds_reject_invalid_input_before_auth(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any]
) -> None:
    import garmin_owl.server as server

    def fail() -> None:
        raise AssertionError("Invalid input must not initialize Garmin")

    monkeypatch.setattr(server, "get_tools", fail)

    async def check() -> None:
        async with Client(mcp) as client:
            result = await client.call_tool(tool, arguments)
            assert result.is_error

    anyio.run(check)


def test_tool_descriptions_route_document_parameters_and_time_convention() -> None:
    timestamped = {
        "get_sleep",
        "get_recovery",
        "get_training_context",
        "get_training_readiness",
        "get_hrv",
        "get_activities",
        "get_activity",
        "get_recent_activities",
        "compare_activities",
        "get_training_week",
        "get_body_composition",
    }

    async def inspect() -> None:
        async with Client(mcp) as client:
            result = await client.list_tools()
            for tool in result.tools:
                description = " ".join((tool.description or "").split())
                assert "Use " in description and "use " in description, tool.name
                assert "not retried automatically" in description, tool.name
                assert "source encoding" not in description, tool.name
                if tool.name in timestamped:
                    assert "UTC offset" in description, tool.name
            by_name = {tool.name: tool for tool in result.tools}
            trend = by_name["get_recovery_trend"].input_schema["properties"]["days"]
            assert (trend["minimum"], trend["maximum"]) == (1, 28)
            assert "28" in trend["description"]
            for name in ("get_stress", "get_body_battery"):
                series = by_name[name].input_schema["properties"]["include_timeseries"]
                assert "48" in series["description"] and "offset" in series["description"]
            assert "whole-day" in (by_name["get_body_battery"].description or "")
            assert "overnight" in (by_name["get_recovery_trend"].description or "")
            week = by_name["get_training_week"].input_schema["properties"]["date"]
            assert "Monday-Sunday" in week["description"]
            sleep_schema = by_name["get_sleep"].output_schema or {}
            assert (
                "never add the offset" in sleep_schema["properties"]["sleep_start"]["description"]
            )

    anyio.run(inspect)


def test_date_format_is_checked_by_the_schema_and_dates_by_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import garmin_owl.server as server
    from garmin_owl.tools import parse_date

    class DateOnly:
        def get_cycle(self, date: str | None) -> dict[str, str]:
            return {"date": parse_date(date).isoformat()}

    monkeypatch.setattr(server, "get_tools", DateOnly)

    async def check() -> None:
        async with Client(mcp) as client:
            listed = {tool.name: tool for tool in (await client.list_tools()).tools}
            for tool in listed.values():
                for name, schema in tool.input_schema.get("properties", {}).items():
                    if name in ("date", "start_date", "end_date"):
                        branch = next(item for item in schema["anyOf"] if item["type"] == "string")
                        assert branch["pattern"] == server.DATE_PATTERN, (tool.name, name)
                        assert (
                            "server's local timezone" in schema["description"]
                            or name == "start_date"
                        )
            wrong_format = await client.call_tool("get_cycle", {"date": "10/10/2026"})
            assert wrong_format.is_error
            impossible = await client.call_tool("get_cycle", {"date": "2026-02-30"})
            text = " ".join(block.text for block in impossible.content if block.type == "text")
            assert impossible.is_error and "not a real calendar date" in text

    anyio.run(check)


def test_activity_listings_declare_an_envelope_with_truncation() -> None:
    async def inspect() -> None:
        async with Client(mcp) as client:
            listed = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in ("get_activities", "get_recent_activities"):
                schema = listed[name].output_schema or {}
                assert {"count", "truncated", "activities"} <= set(schema["properties"]), name
                assert {"count", "truncated", "limit"} <= set(schema.get("required", [])), name
            body = listed["get_body_composition"].output_schema or {}
            assert {"count", "entries"} <= set(body["properties"])

    anyio.run(inspect)
