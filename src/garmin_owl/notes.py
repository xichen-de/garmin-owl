"""The single write path: the description of one existing activity, preview then commit.

Nothing else in garmin-owl can change the Garmin account. Garmin's update endpoint takes only
the fields it should change (verified live: ``{"activityId", "description"}`` alters nothing
else) and answers 204 without a body.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .client import (
    GarminDataClient,
    GarminOwlError,
    GarminOwlMissingDataError,
    GarminOwlRequestError,
    GarminOwlResponseError,
)
from .database import GarminDatabase
from .models import ActivityNoteCommit, ActivityNotePreview, ActivitySummary, AvailabilityNotice
from .normalize import normalize_activity_detail

NoteMode = Literal["append", "replace"]
NOTE_MODES = ("append", "replace")
# Verified live: 2000 is stored, 2001 answers HTTP 400. Garmin counts UTF-16 code units, so a
# Chinese character counts once and most emoji twice.
MAX_DESCRIPTION_UNITS = 2000
WRITES_ENV = "GARMIN_OWL_ENABLE_WRITES"
LOG_ENV = "GARMIN_OWL_NOTES_LOG"
NOT_WRITTEN = "Nothing has been written to Garmin."
_ON = frozenset({"1", "true", "yes", "on"})


class GarminOwlNoteError(GarminOwlError):
    """A note request that was refused, failed, or could not be verified."""


def writes_enabled() -> bool:
    """On unless the setting says otherwise; an unrecognized value counts as off."""
    configured = os.environ.get(WRITES_ENV)
    return configured is None or configured.strip().casefold() in _ON


def notes_log_path() -> Path:
    configured = os.environ.get(LOG_ENV)
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".garmin-owl/activity-notes-log.jsonl"
    )


def _append_log(record: dict[str, Any]) -> None:
    """Durably append one owner-only record without following a planted symlink."""
    path = notes_log_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as output:
        os.fchmod(output.fileno(), stat.S_IRUSR | stat.S_IWUSR)
        output.write(json.dumps(record, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())


@dataclass(frozen=True)
class _Change:
    """One proposed change, computed from a fresh read of the activity."""

    summary: ActivitySummary
    mode: NoteMode
    current: str
    resulting: str

    @property
    def token(self) -> str:
        state = [self.summary.activity_id, self.mode, self.current, self.resulting]
        return hashlib.sha256(json.dumps(state).encode()).hexdigest()

    def identity(self) -> dict[str, Any]:
        return {
            "activity_id": self.summary.activity_id,
            "name": self.summary.name,
            "activity_type": self.summary.activity_type,
            "start_time": self.summary.start_time,
            "duration_seconds": self.summary.duration_seconds,
        }


def _description(raw: Mapping[str, Any]) -> str:
    """Garmin omits the key when there is no note, and stores "" once one was cleared."""
    value = raw.get("description")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise GarminOwlResponseError(
            f"Garmin returned an unreadable description for this activity. {NOT_WRITTEN}"
        )
    return value


class ActivityNotes:
    def __init__(self, client: GarminDataClient, database: GarminDatabase | None = None) -> None:
        self._client = client
        self._database = database
        # MCP runs tools on worker threads: check-then-write must not interleave.
        self._lock = threading.Lock()

    def preview(self, activity_id: int, text: str, mode: NoteMode = "append") -> dict[str, Any]:
        change = self._change(activity_id, text, mode)
        return ActivityNotePreview(
            **change.identity(),
            mode=mode,
            current_description=change.current,
            resulting_description=change.resulting,
            confirmation_token=change.token,
            note=(
                f"{NOT_WRITTEN} Show this preview to the user, and call commit_activity_note "
                "only after they explicitly confirm it."
            ),
        ).compact()

    def commit(
        self,
        activity_id: int,
        text: str,
        confirmation_token: str,
        mode: NoteMode = "append",
    ) -> dict[str, Any]:
        with self._lock:
            change = self._change(activity_id, text, mode)
            if change.token != confirmation_token:
                raise GarminOwlNoteError(
                    "The confirmation token does not match: the activity's description changed "
                    "since the preview, or the inputs differ from it. Call "
                    "preview_activity_note again and have the user confirm the new preview. "
                    f"{NOT_WRITTEN}"
                )
            return self._write(change)

    def _change(self, activity_id: int, text: str, mode: NoteMode) -> _Change:
        if mode not in NOTE_MODES:
            raise GarminOwlNoteError(f"mode must be one of: {', '.join(NOTE_MODES)}")
        text = text.strip()
        if not text:
            raise GarminOwlNoteError(f"text must not be empty. {NOT_WRITTEN}")
        try:
            text.encode()
        except UnicodeEncodeError:
            raise GarminOwlNoteError(f"text is not valid Unicode. {NOT_WRITTEN}") from None
        raw = self._read(activity_id)
        current = _description(raw)
        resulting = f"{current}\n{text}" if mode == "append" and current.strip() else text
        units = len(resulting.encode("utf-16-le", "surrogatepass")) // 2
        if units > MAX_DESCRIPTION_UNITS:
            raise GarminOwlNoteError(
                f"The resulting description would be {units} characters; Garmin accepts at most "
                f"{MAX_DESCRIPTION_UNITS} (most emoji count as two). {NOT_WRITTEN}"
            )
        try:
            summary = normalize_activity_detail(raw).summary
        except ValueError:
            raise GarminOwlResponseError(
                f"Garmin returned an unreadable activity. {NOT_WRITTEN}"
            ) from None
        return _Change(summary, mode, current, resulting)

    def _read(self, activity_id: int) -> Mapping[str, Any]:
        """A fresh, complete read of an activity that is provably the user's own."""
        if isinstance(activity_id, bool) or activity_id <= 0:
            raise GarminOwlNoteError("activity_id must be a positive integer")
        try:
            raw = self._client.activity_summary(activity_id)
        except (GarminOwlMissingDataError, GarminOwlRequestError):
            raise GarminOwlNoteError(
                "Garmin has no such activity, or it is not accessible from this account. "
                f"{NOT_WRITTEN}"
            ) from None
        if not isinstance(raw, Mapping) or raw.get("activityId") != activity_id:
            raise GarminOwlResponseError(
                f"Garmin did not return the requested activity. {NOT_WRITTEN}"
            )
        owner = self._client.profile_id
        if owner is None or raw.get("userProfileId") != owner:
            raise GarminOwlNoteError(
                "This activity could not be confirmed as belonging to the signed-in account. "
                f"{NOT_WRITTEN}"
            )
        return raw

    def _write(self, change: _Change) -> dict[str, Any]:
        activity_id = change.summary.activity_id
        try:
            self._log(change, "pending")
        except OSError as exc:
            raise GarminOwlNoteError(
                f"The audit log could not be written ({type(exc).__name__}). {NOT_WRITTEN}"
            ) from None

        failure: GarminOwlError | None = None
        try:
            self._client.note_writer().set_description(activity_id, change.resulting)
        except GarminOwlError as exc:
            failure = exc
        except Exception as exc:  # the request may have been sent, so never escape unverified
            failure = GarminOwlResponseError(f"The update failed ({type(exc).__name__}).")

        if failure is not None and self._client.is_definite_refusal(failure):
            # Garmin refused the request, so another read cannot add anything.
            self._log_result(change, "failure", str(failure))
            raise GarminOwlNoteError(f"{failure} The note was not written.") from None

        # After success, or an outcome we cannot know (timeout, server error), look once.
        try:
            stored = _description(self._read(activity_id))
        except GarminOwlError as exc:
            self._invalidate(activity_id)
            sent = failure or "update accepted"
            reason = f"unverified: {sent}; read-back failed ({type(exc).__name__})"
            self._log_result(change, "failure", reason)
            raise GarminOwlNoteError(
                "The update was sent but could not be verified, because reading the activity "
                f"back failed. {failure or ''} Check the activity in Garmin Connect before "
                "trying again."
            ) from None

        cache_ok = self._invalidate(activity_id, stored)
        if stored != change.resulting:
            reason = str(failure) if failure else "read-back mismatch"
            self._log_result(change, "failure", reason, stored)
            raise GarminOwlNoteError(
                "The note was not stored as previewed. "
                f"{failure or 'Garmin accepted the update but holds different text.'} "
                f"Garmin now holds this description: {json.dumps(stored, ensure_ascii=False)}"
            ) from None

        logged = self._log_result(change, "success", str(failure) if failure else None)
        notices = []
        if failure is not None:
            notices.append(
                AvailabilityNotice(
                    field="verification",
                    status="confirmed_by_read_back",
                    message="The update request reported an error, but Garmin holds the note.",
                )
            )
        if not cache_ok:
            notices.append(
                AvailabilityNotice(
                    field="cache",
                    status="invalidation_failed",
                    message="Use get_activity with refresh=true to see the new note.",
                )
            )
        if not logged:
            notices.append(
                AvailabilityNotice(
                    field="audit_log",
                    status="result_not_recorded",
                    message="The change was recorded before the write, but not its result.",
                )
            )
        return ActivityNoteCommit(
            **change.identity(),
            mode=change.mode,
            old_description=change.current,
            new_description=stored,
            verified=True,
            availability=notices,
        ).compact()

    def _invalidate(self, activity_id: int, stored: str | None = None) -> bool:
        """A cache problem must never look like a failed write."""
        if self._database is None:
            return True
        try:
            self._database.invalidate_activity_note(activity_id, stored)
        except Exception:
            return False
        return True

    def _log(
        self,
        change: _Change,
        result: str,
        reason: str | None = None,
        stored: str | None = None,
    ) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "activity_id": change.summary.activity_id,
            "activity_name": change.summary.name,
            "start_time": change.summary.start_time,
            "mode": change.mode,
            "old_description": change.current,
            "new_description": change.resulting,
            "result": result,
        }
        if reason is not None:
            record["reason"] = reason
        if stored is not None:
            record["stored_description"] = stored
        _append_log(record)

    def _log_result(
        self,
        change: _Change,
        result: str,
        reason: str | None = None,
        stored: str | None = None,
    ) -> bool:
        try:
            self._log(change, result, reason, stored)
        except OSError:
            return False
        return True
