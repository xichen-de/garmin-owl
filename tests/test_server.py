import anyio
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
