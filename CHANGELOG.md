# Changelog

## 0.5.1

- Connect to Garmin over IPv4 first, with IPv6 as fallback. On networks with broken IPv6, starting up and parallel reads could stall for over a minute and time out.

## 0.5.0

- Add `preview_activity_note` and `commit_activity_note`: add to or replace the description of one existing activity after a confirmed preview. No other field can be changed.
- Writing is **on by default**. Turn it off with the extension's **Allow writing activity notes** setting or `GARMIN_OWL_ENABLE_WRITES=0`; the note tools are then not registered.
- Every write is recorded first in `~/.garmin-owl/activity-notes-log.jsonl`.
