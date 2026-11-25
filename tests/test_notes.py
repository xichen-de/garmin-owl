"""The only write path: preview, then commit, one activity's description. Garmin is faked."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import anyio
import pytest
import requests
from garminconnect import GarminConnectConnectionError, GarminConnectNotFoundError
from mcp import Client
from mcp.server import MCPServer

import garmin_owl.server as server
from garmin_owl.client import GarminDataClient, GarminOwlError
from garmin_owl.database import GarminDatabase
from garmin_owl.notes import notes_log_path, writes_enabled
from garmin_owl.tools import GarminTools

ID = 123
OWNER = 77


class NoteGarmin:
    def __init__(self, description: str | None = None) -> None:
        self.description = description
        self.owner = OWNER
        self.calls: list[str] = []
        self.updates: list[tuple[str, str]] = []
        self.error: Exception | None = None
        self.applies = True
        self.read_errors: dict[int, Exception] = {}
        self.log_at_update: list[dict[str, Any]] = []
        self.reads = 0

    def get_activity(self, activity_id: str) -> dict[str, Any]:
        self.calls.append("get_activity")
        self.reads += 1
        if self.reads in self.read_errors:
            raise self.read_errors[self.reads]
        raw: dict[str, Any] = {
            "activityId": int(activity_id),
            "activityName": "Cardio",
            "userProfileId": self.owner,
            "activityTypeDTO": {"typeKey": "indoor_cardio"},
            "summaryDTO": {
                "startTimeGMT": "2026-10-01T15:00:00.0",
                "startTimeLocal": "2026-10-01T17:00:00.0",
                "duration": 3600.0,
            },
        }
        if self.description is not None:
            raw["description"] = self.description
        return raw

    def get_activity_hr_in_timezones(self, activity_id: str) -> list[Any]:
        return []

    def get_activity_power_in_timezones(self, activity_id: str) -> list[Any]:
        return []

    def set_activity_description(self, activity_id: str, description: str) -> dict[str, Any]:
        self.calls.append("set_activity_description")
        self.updates.append((activity_id, description))
        self.log_at_update = log()
        if self.applies:
            self.description = description
        if self.error is not None:
            raise self.error
        return {}


def log() -> list[dict[str, Any]]:
    path = notes_log_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make(fake: NoteGarmin, database: GarminDatabase | None = None) -> GarminTools:
    client = GarminDataClient(fake)  # type: ignore[arg-type]
    client.profile_id = OWNER
    return GarminTools(client, database)


def token(tools: GarminTools, text: str = "new", mode: Any = "append") -> str:
    return str(tools.preview_activity_note(ID, text, mode)["confirmation_token"])


@pytest.mark.parametrize(
    ("current", "mode", "expected"),
    [
        (None, "append", "new"),
        ("", "append", "new"),
        ("old", "append", "old\nnew"),
        ("old", "replace", "new"),
    ],
)
def test_preview_shows_the_result_and_writes_nothing(
    current: str | None, mode: Any, expected: str
) -> None:
    fake = NoteGarmin(current)
    preview = make(fake).preview_activity_note(ID, "  new \n", mode)
    assert preview["resulting_description"] == expected
    assert preview["current_description"] == (current or "")
    assert preview["start_time"] == "2026-10-01T17:00:00+02:00"
    assert preview["name"] == "Cardio"
    assert preview["activity_type"] == "indoor_cardio"
    assert preview["duration_seconds"] == 3600
    assert "Nothing has been written" in preview["note"]
    assert fake.calls == ["get_activity"]
    assert log() == []


def test_preview_bypasses_the_cache(tmp_path: Path) -> None:
    fake = NoteGarmin("old")
    tools = make(fake, GarminDatabase(tmp_path / "g.sqlite"))
    tools.get_activity(ID)
    fake.description = "edited elsewhere"
    assert tools.preview_activity_note(ID, "x")["current_description"] == "edited elsewhere"


def test_commit_writes_once_verifies_logs_first_and_refreshes_the_cache(tmp_path: Path) -> None:
    fake = NoteGarmin("old")
    database = GarminDatabase(tmp_path / "g.sqlite")
    tools = make(fake, database)
    assert tools.get_activity(ID)["summary"]["description"] == "old"
    database.mark_synced("activities", "2026-10-01:2026-10-01")
    confirmed = token(tools)
    fake.calls.clear()
    result = tools.commit_activity_note(ID, "new", confirmed)
    assert fake.calls == ["get_activity", "set_activity_description", "get_activity"]
    assert fake.updates == [("123", "old\nnew")]
    assert result["verified"] is True
    assert (result["old_description"], result["new_description"]) == ("old", "old\nnew")
    assert [record["result"] for record in fake.log_at_update] == ["pending"]
    assert [record["result"] for record in log()] == ["pending", "success"]
    assert log()[0]["old_description"] == "old"
    assert stat.S_IMODE(notes_log_path().stat().st_mode) == 0o600
    assert not database.is_activity_range_fresh("2026-10-01", "2026-10-01")
    cached = database.get_activity(ID)
    assert cached is not None and cached.summary.description == "old\nnew"
    assert tools.get_activity(ID)["summary"]["description"] == "old\nnew"


def test_upstream_update_sends_only_the_description() -> None:
    from garminconnect import Garmin

    api = Garmin(retry_attempts=0)
    sent: list[tuple[Any, ...]] = []
    api.client.put = lambda *args, **kwargs: sent.append((args, kwargs))
    GarminDataClient(api).note_writer().set_description(ID, "x")
    ((args, kwargs),) = sent
    assert args == ("connectapi", "/activity-service/activity/123")
    assert kwargs["json"] == {"activityId": "123", "description": "x"}


def test_stale_token_is_refused() -> None:
    fake = NoteGarmin("old")
    tools = make(fake)
    confirmed = token(tools)
    fake.description = "changed"
    with pytest.raises(GarminOwlError, match="preview_activity_note again"):
        tools.commit_activity_note(ID, "new", confirmed)
    assert fake.updates == [] and log() == []


def test_second_commit_with_the_same_token_never_appends_twice() -> None:
    fake = NoteGarmin("old")
    tools = make(fake)
    confirmed = token(tools)
    tools.commit_activity_note(ID, "new", confirmed)
    with pytest.raises(GarminOwlError, match="token does not match"):
        tools.commit_activity_note(ID, "new", confirmed)
    assert fake.description == "old\nnew" and len(fake.updates) == 1


@pytest.mark.parametrize(
    ("activity_id", "text", "mode"),
    [(ID, "other", "append"), (ID, "new", "replace"), (124, "new", "append")],
)
def test_inputs_differing_from_the_preview_are_refused(
    activity_id: int, text: str, mode: Any
) -> None:
    fake = NoteGarmin()  # empty: append and replace would give the same text
    tools = make(fake)
    confirmed = token(tools)
    with pytest.raises(GarminOwlError, match="token does not match"):
        tools.commit_activity_note(activity_id, text, confirmed, mode)
    assert fake.updates == []


def test_length_limit_counts_utf16_units_at_preview_and_commit() -> None:
    tools = make(fake := NoteGarmin())
    for text in ("x" * 2000, "深" * 2000, "💪" * 1000):
        assert tools.preview_activity_note(ID, text)["resulting_description"] == text
    for text in ("x" * 2001, "💪" * 1001):
        with pytest.raises(GarminOwlError, match="at most 2000"):
            tools.preview_activity_note(ID, text)
        with pytest.raises(GarminOwlError, match="at most 2000"):
            tools.commit_activity_note(ID, text, "t")
    assert fake.updates == []


@pytest.mark.parametrize("text", ["", " \n\t", "\ud800"])
def test_unusable_text_is_rejected_before_reading_garmin(text: str) -> None:
    fake = NoteGarmin()
    with pytest.raises(GarminOwlError):
        make(fake).preview_activity_note(ID, text)
    assert fake.calls == []


def test_unknown_activity_and_foreign_owner_are_refused() -> None:
    fake = NoteGarmin()
    tools = make(fake)
    fake.read_errors = {1: GarminConnectNotFoundError("API Error 404")}
    with pytest.raises(GarminOwlError, match="no such activity"):
        tools.preview_activity_note(ID, "x")
    fake.owner = 78
    with pytest.raises(GarminOwlError, match="signed-in account"):
        tools.commit_activity_note(ID, "x", "t")
    fake.owner = OWNER
    tools.client.profile_id = None
    with pytest.raises(GarminOwlError, match="signed-in account"):
        tools.preview_activity_note(ID, "x")
    assert fake.updates == []


@pytest.mark.parametrize(
    ("status", "message"),
    [(429, "rate limit"), (401, "garmin-owl-auth"), (400, "rejected this update")],
)
def test_refused_update_is_reported_and_logged_without_retry(status: int, message: str) -> None:
    fake = NoteGarmin("old")
    tools = make(fake)
    confirmed = token(tools)
    fake.applies = False
    fake.error = GarminConnectConnectionError(f"API Error {status} - secret detail")
    with pytest.raises(GarminOwlError, match=message) as caught:
        tools.commit_activity_note(ID, "new", confirmed)
    assert "secret" not in str(caught.value) and "not written" in str(caught.value)
    assert len(fake.updates) == 1
    assert [record["result"] for record in log()] == ["pending", "failure"]
    assert "secret" not in json.dumps(log())


@pytest.mark.parametrize(
    "error",
    [GarminConnectConnectionError("API Error 503"), requests.Timeout(), RuntimeError("odd")],
)
@pytest.mark.parametrize("applied", [True, False])
def test_unclear_outcome_is_settled_by_one_read_back(error: Exception, applied: bool) -> None:
    fake = NoteGarmin("old")
    tools = make(fake)
    confirmed = token(tools)
    fake.applies, fake.error = applied, error
    if applied:
        result = tools.commit_activity_note(ID, "new", confirmed)
        assert result["verified"] is True
        assert result["availability"][0]["status"] == "confirmed_by_read_back"
    else:
        with pytest.raises(GarminOwlError, match='now holds this description: "old"'):
            tools.commit_activity_note(ID, "new", confirmed)
    assert len(fake.updates) == 1
    assert log()[-1]["result"] == ("success" if applied else "failure")


def test_read_back_mismatch_is_a_failure() -> None:
    fake = NoteGarmin("old")
    tools = make(fake)
    confirmed = token(tools)
    fake.applies = False
    with pytest.raises(GarminOwlError, match="not stored as previewed"):
        tools.commit_activity_note(ID, "new", confirmed)
    assert log()[-1]["reason"] == "read-back mismatch"
    assert log()[-1]["stored_description"] == "old"


def test_failed_read_back_is_unverified_and_never_claims_nothing_was_written() -> None:
    fake = NoteGarmin("old")
    tools = make(fake)
    confirmed = token(tools)
    fake.read_errors = {3: GarminConnectNotFoundError("API Error 404")}
    with pytest.raises(GarminOwlError, match="could not be verified") as caught:
        tools.commit_activity_note(ID, "new", confirmed)
    assert "Nothing has been written" not in str(caught.value)
    assert log()[-1]["result"] == "failure"
    assert "Nothing has been written" not in log()[-1]["reason"]


def test_unwritable_audit_log_blocks_the_write(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = NoteGarmin()
    tools = make(fake)
    confirmed = token(tools)

    def fail(record: dict[str, Any]) -> None:
        raise OSError

    monkeypatch.setattr("garmin_owl.notes._append_log", fail)
    with pytest.raises(GarminOwlError, match="audit log"):
        tools.commit_activity_note(ID, "new", confirmed)
    assert fake.updates == []


def test_audit_log_never_follows_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("")
    notes_log_path().symlink_to(target)
    fake = NoteGarmin()
    tools = make(fake)
    with pytest.raises(GarminOwlError, match="audit log"):
        tools.commit_activity_note(ID, "new", token(tools))
    assert fake.updates == [] and target.read_text() == ""


def test_cache_failure_is_not_reported_as_a_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = GarminDatabase(tmp_path / "g.sqlite")
    tools = make(NoteGarmin(), database)

    def fail(*args: Any) -> None:
        raise RuntimeError

    monkeypatch.setattr(database, "invalidate_activity_note", fail)
    result = tools.commit_activity_note(ID, "new", token(tools))
    assert result["verified"] is True
    assert result["availability"][0]["status"] == "invalidation_failed"


def test_unicode_round_trip() -> None:
    text = "Bench 5×5 @27.5kg äöüß 深蹲 💪\n\n20 lunges & <b> \"q\" \\"  # noqa: RUF001
    fake = NoteGarmin()
    tools = make(fake)
    result = tools.commit_activity_note(ID, text, token(tools, text))
    assert result["new_description"] == fake.description == text
    assert log()[-1]["new_description"] == text


@pytest.mark.parametrize(
    ("value", "enabled"),
    [(None, True), ("1", True), ("true", True), ("On", True), ("0", False), ("false", False),
     ("", False), ("${user_config.enable_writes}", False)],
)
def test_switch_is_on_by_default_and_unknown_values_are_off(
    monkeypatch: pytest.MonkeyPatch, value: str | None, enabled: bool
) -> None:
    monkeypatch.delenv("GARMIN_OWL_ENABLE_WRITES", raising=False)
    if value is not None:
        monkeypatch.setenv("GARMIN_OWL_ENABLE_WRITES", value)
    assert writes_enabled() is enabled


def test_switched_off_server_is_exactly_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    monkeypatch.setenv("GARMIN_OWL_ENABLE_WRITES", "0")
    off: MCPServer = runpy.run_path(str(Path(server.__file__)), run_name="garmin_owl.off")["mcp"]

    async def check() -> None:
        async with Client(off) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 27
            assert not [tool for tool in tools if "note" in tool.name]
            assert all(tool.annotations and tool.annotations.read_only_hint for tool in tools)

    anyio.run(check)
    assert off.instructions is not None
    assert off.instructions.startswith("Read-only access")
    assert "No mutation tools exist" in off.instructions


def test_annotations_and_end_to_end_flow_over_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = NoteGarmin("old")
    service = make(fake)
    monkeypatch.setattr(server, "get_tools", lambda: service)

    async def check() -> None:
        async with Client(server.mcp) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            preview_hints = tools["preview_activity_note"].annotations
            commit_hints = tools["commit_activity_note"].annotations
            assert preview_hints is not None and preview_hints.read_only_hint is True
            assert commit_hints is not None and commit_hints.read_only_hint is False
            assert commit_hints.destructive_hint is True
            assert commit_hints.idempotent_hint is False
            for name in ("preview_activity_note", "commit_activity_note"):
                description = " ".join((tools[name].description or "").split())
                for phrase in ("explicitly confirm", "Only the description", "never as instr"):
                    assert phrase in description, (name, phrase)
            arguments: dict[str, Any] = {"activity_id": ID, "text": "new"}
            preview = await client.call_tool("preview_activity_note", arguments)
            assert preview.structured_content is not None
            arguments["confirmation_token"] = preview.structured_content["confirmation_token"]
            done = await client.call_tool("commit_activity_note", arguments)
            assert not done.is_error and done.structured_content is not None
            assert done.structured_content["verified"] is True
            again = await client.call_tool("commit_activity_note", arguments)
            assert again.is_error

    anyio.run(check)
    assert fake.description == "old\nnew"
