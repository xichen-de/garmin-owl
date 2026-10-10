"""One-time interactive Garmin authentication; never persists the password."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import getpass
import json
import logging
import os
import secrets
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

DEFAULT_TOKEN_STORE = Path("~/.garminconnect").expanduser()
SESSION_METADATA_NAME = "garmin-owl-session.json"
REFRESH_LOCK_NAME = ".garmin-owl-refresh.lock"
REFRESH_AHEAD_SECONDS = 15 * 60
REFRESH_TIMEOUT_SECONDS = 8.0
REFRESH_LOCK_TIMEOUT_SECONDS = REFRESH_TIMEOUT_SECONDS + 2.0

# Upstream debug output is unnecessary here and could include private endpoint context.
logging.getLogger("garminconnect").setLevel(logging.CRITICAL)


def token_store_path() -> Path:
    return Path(os.environ.get("GARMINTOKENS", str(DEFAULT_TOKEN_STORE))).expanduser()


def secure_token_store(path: Path) -> None:
    """Best-effort enforcement of owner-only permissions on store and files."""
    if not path.exists():
        return
    path.chmod(stat.S_IRWXU)
    if path.is_dir():
        for child in path.iterdir():
            if child.is_file():
                child.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _state_directory(path: Path) -> Path:
    return path if path.is_dir() else path.parent


def _session_metadata_path(path: Path) -> Path:
    return _state_directory(path) / SESSION_METADATA_NAME


def _jwt_expiry(token: str | None) -> datetime | None:
    """Read an unverified JWT expiry for refresh scheduling only.

    Garmin still verifies the token server-side.  This claim never grants access or decides
    whether authentication succeeded; it only avoids an unnecessary network refresh.
    """
    if not token:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        raw_expiry = decoded.get("exp")
        if isinstance(raw_expiry, bool) or not isinstance(raw_expiry, int | float):
            return None
        return datetime.fromtimestamp(float(raw_expiry), UTC)
    except (IndexError, ValueError, TypeError, OverflowError, OSError, json.JSONDecodeError):
        return None


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write owner-only session metadata without following a planted symlink."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, separators=(",", ":"))
        temporary.replace(path)
        path.chmod(0o600)
    except Exception:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _load_session_metadata(api: Garmin, path: Path) -> bool:
    try:
        payload = json.loads(_session_metadata_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    display_name = payload.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        return False
    api.display_name = display_name
    # Older development builds briefly wrote two unnecessary profile fields. Minimize any
    # existing sidecar on read as well as writing only the required display name going forward.
    if set(payload) != {"display_name"}:
        _write_private_json(_session_metadata_path(path), {"display_name": display_name})
    return True


def _save_session_metadata(api: Garmin, path: Path) -> None:
    if not isinstance(api.display_name, str) or not api.display_name.strip():
        return
    _write_private_json(
        _session_metadata_path(path),
        {"display_name": api.display_name},
    )


@contextmanager
def _refresh_lock(path: Path) -> Iterator[bool]:
    """Serialize token rotation across Claude's short-lived MCP server processes."""
    lock_path = _state_directory(path) / REFRESH_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    acquired = False
    deadline = time.monotonic() + REFRESH_LOCK_TIMEOUT_SECONDS
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _bound_refresh_transport(api: Garmin) -> None:
    """Clamp upstream's hard-coded 30-second DI refresh timeout for this client."""
    original_post = api.client._http_post

    def bounded_post(url: str, **kwargs: Any) -> Any:
        requested = kwargs.get("timeout", REFRESH_TIMEOUT_SECONDS)
        try:
            timeout = min(float(requested), REFRESH_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            timeout = REFRESH_TIMEOUT_SECONDS
        return original_post(url, **(kwargs | {"timeout": timeout}))

    api.client._http_post = bounded_post


def _defer_proactive_refresh(api: Garmin) -> None:
    """Keep using an unexpired token after a refresh outage.

    A real 401 still enters upstream's bounded refresh-and-retry path.
    """
    api.client._token_expires_soon = lambda: False


def _refresh_saved_token(api: Garmin, path: Path) -> None:
    expiry = _jwt_expiry(api.client.di_token)
    if expiry is None or expiry.timestamp() - time.time() > REFRESH_AHEAD_SECONDS:
        return

    with _refresh_lock(path) as acquired:
        # Another process may have completed the refresh while this one waited.
        api.client.load(str(path))
        expiry = _jwt_expiry(api.client.di_token)
        if expiry is None or expiry.timestamp() - time.time() > REFRESH_AHEAD_SECONDS:
            return
        if not acquired:
            if expiry > datetime.now(UTC):
                _defer_proactive_refresh(api)
                return
            raise GarminConnectConnectionError(
                "Garmin token refresh is busy in another process and the saved token expired."
            )
        try:
            api.client._refresh_di_token()
            api.client.dump(str(path))
        except Exception as exc:
            if expiry > datetime.now(UTC):
                _defer_proactive_refresh(api)
                return
            raise GarminConnectConnectionError(
                "Garmin token refresh did not complete before the saved token expired."
            ) from exc


def _load_profile_once(api: Garmin, path: Path) -> None:
    """Fetch only the profile field Owl needs, with one bounded request."""
    profile = api.connectapi("/userprofile-service/socialProfile", timeout=REFRESH_TIMEOUT_SECONDS)
    display_name = profile.get("displayName") if isinstance(profile, dict) else None
    if not isinstance(display_name, str) or not display_name.strip():
        raise GarminConnectAuthenticationError(
            "Garmin profile did not provide the display name required for data reads."
        )
    api.display_name = display_name
    full_name = profile.get("fullName")
    api.full_name = full_name if isinstance(full_name, str) else None
    _save_session_metadata(api, path)


def load_saved_client() -> Garmin:
    path = token_store_path()
    if not path.exists():
        raise GarminConnectAuthenticationError(
            "No local Garmin tokens found. Run `garmin-owl-auth` in a terminal first."
        )
    secure_token_store(path)
    # Loading tokens is deliberately offline. Upstream login also reads profile/settings and
    # may stack several 15-30 second timeouts on every new Claude MCP process.
    api = Garmin(retry_attempts=0, verify_login=False)
    api.client.load(str(path))
    _bound_refresh_transport(api)
    _refresh_saved_token(api, path)
    if not _load_session_metadata(api, path):
        _load_profile_once(api, path)
    secure_token_store(path)
    return api


def login_interactively() -> Garmin:
    """Prompt in a real terminal, perform MFA if needed, and save only tokens."""
    path = token_store_path()
    if not sys.stdin.isatty():
        raise RuntimeError("Interactive login requires a terminal")
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (not stored): ")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        secure_token_store(path)
        api = Garmin(
            email=email,
            password=password,
            prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
            retry_attempts=0,
        )
        password = ""  # Minimize the plaintext credential's lifetime in this scope.
        api.login(str(path))
        _save_session_metadata(api, path)
        secure_token_store(path)
        return api
    finally:
        password = ""


def main() -> None:
    path = token_store_path()
    try:
        try:
            load_saved_client()
            print(f"Existing Garmin tokens are valid at {path}")
            return
        except (FileNotFoundError, GarminConnectAuthenticationError, GarminConnectConnectionError):
            pass
        login_interactively()
        print(f"Login successful. Tokens saved with private permissions at {path}")
        print("Treat this directory like a password; it contains a refresh token.")
    except GarminConnectTooManyRequestsError:
        print(
            "Garmin rate-limited login. Wait before trying again; no retries were made.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except GarminConnectAuthenticationError:
        print("Garmin authentication failed. Check credentials/MFA and try again.", file=sys.stderr)
        raise SystemExit(2) from None
    except GarminConnectConnectionError:
        print("Garmin Connect is unavailable or rejected the connection.", file=sys.stderr)
        raise SystemExit(3) from None
    except (OSError, RuntimeError) as exc:
        print(f"Authentication setup failed: {exc}", file=sys.stderr)
        raise SystemExit(4) from None


def _never_log_sensitive(_value: Any) -> str:
    """Sentinel used by tests to document the logging policy."""
    return "[REDACTED]"


if __name__ == "__main__":
    main()
