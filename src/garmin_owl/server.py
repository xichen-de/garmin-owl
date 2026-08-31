"""Local stdio MCP transport for garmin-owl."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from .tools import GarminTools

mcp = MCPServer(
    "garmin-owl",
    version="0.1.0",
    instructions=(
        "Read-only access to the local user's Garmin Connect data. "
        "Never claim this is medical advice. No mutation tools exist."
    ),
    log_level="WARNING",
)

_tools: GarminTools | None = None


def get_tools() -> GarminTools:
    """Authenticate lazily so importing/listing the server never prompts or hits Garmin."""
    global _tools
    if _tools is None:
        _tools = GarminTools()
    return _tools


@mcp.tool()
def get_daily_summary(date: str | None = None) -> dict[str, Any]:
    """Get concise daily steps, distance, calories, HR, stress, floors, and intensity."""
    return get_tools().get_daily_summary(date)


@mcp.tool()
def get_sleep(date: str | None = None) -> dict[str, Any]:
    """Get Garmin-recorded sleep score, stages, timing, respiration, and SpO2 when available."""
    return get_tools().get_sleep(date)


@mcp.tool()
def get_hrv(date: str | None = None, include_timeseries: bool = False) -> dict[str, Any]:
    """Get Garmin HRV status and nightly values; optional readings are capped at 48 points."""
    return get_tools().get_hrv(date, include_timeseries)


@mcp.tool()
def get_recovery(date: str | None = None) -> dict[str, Any]:
    """Combine Garmin sleep, HRV, Body Battery, stress, RHR, readiness; states why any is absent."""
    return get_tools().get_recovery(date)


@mcp.tool()
def get_training_readiness(date: str | None = None) -> dict[str, Any]:
    """Get Garmin training readiness and its available components."""
    return get_tools().get_training_readiness(date)


@mcp.tool()
def get_body_battery(date: str | None = None, include_timeseries: bool = False) -> dict[str, Any]:
    """Get Garmin Body Battery charged/drained and start/end/high/low levels for one day."""
    return get_tools().get_body_battery(date, include_timeseries)


@mcp.tool()
def get_stress(date: str | None = None, include_timeseries: bool = False) -> dict[str, Any]:
    """Get Garmin daily average/max stress and per-band durations; series capped at 48 points."""
    return get_tools().get_stress(date, include_timeseries)


@mcp.tool()
def get_activities(
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List concise activities in an inclusive date range; defaults to the last 14 days."""
    return get_tools().get_activities(start_date, end_date, limit)


@mcp.tool()
def get_activity(activity_id: int) -> dict[str, Any]:
    """Get one activity summary, laps, training effect, and available HR/power zones."""
    return get_tools().get_activity(activity_id)


@mcp.tool()
def get_body_composition(
    start_date: str | None = None, end_date: str | None = None
) -> list[dict[str, Any]]:
    """Get weight and Garmin-provided body composition for at most 366 days."""
    return get_tools().get_body_composition(start_date, end_date)


@mcp.tool()
def get_training_context(date: str | None = None) -> dict[str, Any]:
    """Get recovery and preceding training anchored to date, with transparent comparisons."""
    return get_tools().get_training_context(date)


@mcp.tool()
def get_recovery_trend(days: int = 7) -> dict[str, Any]:
    """Trend sleep, HRV, RHR, readiness, and Body Battery over 7/14/28 days with coverage data."""
    return get_tools().get_recovery_trend(days)


@mcp.tool()
def get_training_week(date: str | None = None) -> dict[str, Any]:
    """Summarize a Mon-Sun week; every total discloses how many activities reported the metric."""
    return get_tools().get_training_week(date)


@mcp.tool()
def get_training_load(date: str | None = None) -> dict[str, Any]:
    """Get Garmin training status/load, VO2 max, endurance, hill; names unavailable sources."""
    return get_tools().get_training_load(date)


@mcp.tool()
def get_recent_activities(
    days: int = 14,
    activity_type: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List recent cached activities with bounded days/limit and optional exact type filter."""
    return get_tools().get_recent_activities(days, activity_type, limit)


@mcp.tool()
def compare_activities(activity_ids: list[int]) -> dict[str, Any]:
    """Compare 2-10 activities; each metric range states how many reported a value."""
    return get_tools().compare_activities(activity_ids)


@mcp.tool()
def get_cycle(date: str | None = None) -> dict[str, Any]:
    """Get normalized cycle phase/timing without notes, symptoms, or raw day logs."""
    return get_tools().get_cycle(date)


def main() -> None:
    """Run stdio only; this package intentionally exposes no network listener."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
