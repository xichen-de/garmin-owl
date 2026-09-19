from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest
from garminconnect import GarminConnectConnectionError

import garmin_owl.auth as auth


def _token(expiry: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class FakeClient:
    def __init__(self, token: str, *, refresh_fails: bool = False) -> None:
        self.di_token = token
        self.di_refresh_token = "refresh"
        self.di_client_id = "client"
        self.refresh_fails = refresh_fails
        self.loads = 0
        self.refreshes = 0
        self.dumps = 0
        self._http_post = lambda url, **kwargs: None

    def load(self, path: str) -> None:
        self.loads += 1

    def dump(self, path: str) -> None:
        self.dumps += 1

    def _refresh_di_token(self) -> None:
        self.refreshes += 1
        if self.refresh_fails:
            raise TimeoutError("private upstream detail")
        self.di_token = _token(time.time() + 3600)

    def _token_expires_soon(self) -> bool:
        return True


class FakeGarmin:
    clients: ClassVar[list[FakeClient]] = []
    token = _token(time.time() + 3600)
    refresh_fails = False
    profile_calls = 0

    def __init__(self, **kwargs: Any) -> None:
        self.client = FakeClient(self.token, refresh_fails=self.refresh_fails)
        self.clients.append(self.client)
        self.display_name: str | None = None
        self.full_name: str | None = None
        self.unit_system: str | None = None

    def connectapi(self, path: str, **kwargs: Any) -> dict[str, str]:
        type(self).profile_calls += 1
        return {"displayName": "cached-user", "fullName": "Private Name"}


@pytest.fixture(autouse=True)
def reset_fake() -> None:
    FakeGarmin.clients = []
    FakeGarmin.token = _token(time.time() + 3600)
    FakeGarmin.refresh_fails = False
    FakeGarmin.profile_calls = 0


def _token_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = tmp_path / "tokens"
    store.mkdir()
    (store / "garmin_tokens.json").write_text("{}")
    monkeypatch.setenv("GARMINTOKENS", str(store))
    monkeypatch.setattr(auth, "Garmin", FakeGarmin)
    return store


def test_saved_client_starts_offline_with_cached_session_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _token_store(tmp_path, monkeypatch)
    auth._write_private_json(
        store / auth.SESSION_METADATA_NAME,
        {"display_name": "cached-user"},
    )

    api = auth.load_saved_client()

    assert api.display_name == "cached-user"
    assert FakeGarmin.profile_calls == 0
    assert FakeGarmin.clients[0].refreshes == 0


def test_near_expiry_token_is_refreshed_once_and_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _token_store(tmp_path, monkeypatch)
    FakeGarmin.token = _token(time.time() + 60)
    auth._write_private_json(
        store / auth.SESSION_METADATA_NAME,
        {"display_name": "cached-user"},
    )

    auth.load_saved_client()

    client = FakeGarmin.clients[0]
    assert client.refreshes == 1
    assert client.dumps == 1


def test_refresh_failure_uses_still_valid_token_without_a_second_proactive_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _token_store(tmp_path, monkeypatch)
    FakeGarmin.token = _token(time.time() + 60)
    FakeGarmin.refresh_fails = True
    auth._write_private_json(
        store / auth.SESSION_METADATA_NAME,
        {"display_name": "cached-user"},
    )

    auth.load_saved_client()

    client = FakeGarmin.clients[0]
    assert client.refreshes == 1
    assert client._token_expires_soon() is False


def test_expired_token_refresh_failure_is_fast_and_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _token_store(tmp_path, monkeypatch)
    FakeGarmin.token = _token(time.time() - 60)
    FakeGarmin.refresh_fails = True

    with pytest.raises(GarminConnectConnectionError, match="saved token expired"):
        auth.load_saved_client()


def test_profile_is_fetched_once_then_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _token_store(tmp_path, monkeypatch)

    first = auth.load_saved_client()
    second = auth.load_saved_client()

    assert first.display_name == second.display_name == "cached-user"
    assert FakeGarmin.profile_calls == 1
    assert (store / auth.SESSION_METADATA_NAME).stat().st_mode & 0o777 == 0o600
