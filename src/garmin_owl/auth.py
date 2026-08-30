"""One-time interactive Garmin authentication; never persists the password."""

from __future__ import annotations

import getpass
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

DEFAULT_TOKEN_STORE = Path("~/.garminconnect").expanduser()

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


def load_saved_client() -> Garmin:
    path = token_store_path()
    if not path.exists():
        raise GarminConnectAuthenticationError(
            "No local Garmin tokens found. Run `garmin-owl-auth` in a terminal first."
        )
    secure_token_store(path)
    api = Garmin(retry_attempts=0)
    api.login(str(path))
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
