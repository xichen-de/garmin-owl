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
