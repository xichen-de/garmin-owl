# garmin-owl

`garmin-owl` is a local-only, read-only Garmin MCP server for macOS. It gives an MCP client such as Claude Desktop concise health and training context without uploading a separate copy of your Garmin data or adding any Garmin write capability.

It uses the unofficial [`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect) client, so Garmin API changes may occasionally require updates.

## Quick start

Requirements: macOS, Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and a Garmin Connect account.

```bash
git clone https://github.com/xichen-de/garmin-owl.git
cd garmin-owl
uv sync
uv run garmin-owl-auth
```

Authentication happens in your terminal: you'll be prompted for your Garmin email, password, and (if your account has it enabled) an MFA code. The MCP server never asks the model for any of these. Nothing but a reusable session token is saved, by `python-garminconnect`, to `~/.garminconnect` (override with the `GARMINTOKENS` environment variable). Re-run `uv run garmin-owl-auth` any time to check whether the saved tokens are still valid — it re-authenticates only if they aren't.

#### Why does this ask for my Garmin password?

Garmin has no public OAuth login for personal Garmin Connect data — the kind of "Sign in with Garmin" redirect flow you'd get from Google or GitHub simply doesn't exist for individual accounts. Garmin's only OAuth API (the Connect Developer Program) is gated behind a business partnership agreement, not available to a personal script. So `garmin-owl`, like every other open-source tool that reads personal Garmin data, authenticates the same way the official Garmin Connect app and website do internally: your email and password go directly to Garmin's own login endpoint (`sso.garmin.com`) in exchange for session tokens, and only those tokens are stored locally. Your credentials are typed once in your own terminal, held only in memory for that one login call, and never logged, written to disk, or sent to anything other than Garmin's own servers — in particular, they never reach the MCP client or the model. See [Privacy and safety](#privacy-and-safety) for what is stored and where.

## Install in Claude Desktop

1. Authenticate once with `uv run garmin-owl-auth`.
2. Open Claude Desktop → **Settings** → **Extensions**.
3. Drag the latest `garmin-owl-*.mcpb` file from `dist/` into the Extensions window.
4. Enable the extension and restart Claude Desktop if prompted.

The bundle contains the project source, but no credentials, tokens, cookies, or health data. It launches its installed copy with:

```text
uv --directory <installed-extension-directory> run garmin-owl
```

After installing, try:

- “Summarize my recovery today.”
- “How did I sleep last night?”
- “Compare my last two activities.”
- “Show my 14-day recovery trend.”
- “How does my cycle day affect my recovery?”

## Available tools

The server exposes 17 read-only MCP tools:

| Tool | Answers |
| --- | --- |
| `get_daily_summary` | Steps, distance, calories, HR, stress, floors, intensity minutes for one day |
| `get_sleep` | Sleep score, stages, timing, respiration, SpO2 |
| `get_hrv` | HRV status and nightly/weekly averages |
| `get_body_battery` | Daily Body Battery charged/drained and start/end/highest/lowest levels |
| `get_stress` | Daily stress durations by intensity band |
| `get_training_readiness` | Garmin's readiness score and its components |
| `get_recovery` | Sleep, HRV, Body Battery, stress, RHR, and readiness combined for one day, with a stated reason for any absent component |
| `get_recovery_trend` | Recovery facts and current-date comparisons with preceding Garmin days |
| `get_training_context` | Recovery plus the requested date's preceding training and comparisons |
| `get_activities` | Activities in a date range, up to 100 (defaults to the last 14 days) |
| `get_recent_activities` | Activities from the last N days, optionally filtered by type |
| `get_activity` | One activity's laps, training effect, and HR/power zones |
| `compare_activities` | Side-by-side metrics for 2-10 activities |
| `get_training_week` | Totals and available zone time for the Mon-Sun week, with per-metric activity coverage |
| `get_training_load` | Training status, VO2 max, endurance, and hill scores |
| `get_body_composition` | Weight and related measurements over a date range |
| `get_cycle` | Cycle phase, day, and Garmin predictions (fetched only when asked) |

The normalized response for `get_cycle` intentionally excludes notes, symptoms, moods, sexual activity, and raw daily logs.

All output is concise and normalized. Missing device metrics remain missing rather than being
guessed, and are never summed as zero: a weekly total covers only the activities that reported the
metric and says how many that was. Derived comparisons identify their current date, preceding
baseline range, sample count, and calculation, and values garmin-owl calculates rather than reads
from Garmin are labelled as such in an `availability` list. That list also distinguishes *why*
something is absent — Garmin had no data, the metric is unsupported, or the read failed or was
rate-limited — so an unavailable value is never reported as a measured zero or a silent gap.

## Sync and local cache

The optional sync command makes later MCP requests faster:

```bash
uv run garmin-owl-sync
uv run garmin-owl-sync --days 30
```

The default sync loads the last seven days of daily summaries, sleep, HRV, training readiness, and activity summaries. It fetches only missing or stale records. Because watches and scales upload late, a calendar day is treated as settled only at noon the following day: a record is trusted indefinitely once it was *fetched* after its day settled, and a record captured while the day was still synchronizing is reused for at most 20 minutes and then re-fetched. Body Battery, stress, activity details, and cycle data stay on demand and are cached under the same rule.

To inspect or clear the cache:

```bash
uv run garmin-owl-cache-info
uv run garmin-owl-cache-clear
```

The SQLite cache defaults to `~/Library/Application Support/garmin-owl/garmin.sqlite`. Set `GARMIN_OWL_DB` to use another location. Clearing the cache does not remove Garmin authentication tokens.

## Troubleshooting

`garmin-owl` never retries automatically and never surfaces raw Garmin responses, so error messages are short. Here's what each one means and how to fix it:

| Message contains | Meaning | Fix |
| --- | --- | --- |
| "No local Garmin tokens found" | You haven't authenticated yet, or `~/.garminconnect` was deleted | Run `uv run garmin-owl-auth` in a terminal |
| "authentication expired or was rejected" | Garmin logged the session out (e.g. after a password change) | Run `uv run garmin-owl-auth` again to re-authenticate |
| "rate limit reached" | Too many Garmin requests too quickly | Wait a few minutes before retrying; running `garmin-owl-sync` less often also helps |
| "Garmin Connect is unavailable or rejected this read request" | A transient network or Garmin outage | Try again later |
| "unexpected response shape" | Garmin changed a private endpoint's fields | [Open an issue](https://github.com/xichen-de/garmin-owl/issues) with the tool name (never paste your Garmin data) |
| "no data for this request" | That metric isn't recorded for that date/device (e.g. no compatible sensor) | Expected for unsupported metrics; not an error to fix |

If the extension doesn't appear in Claude Desktop after installing, confirm the `.mcpb` file matches the `dist/garmin-owl-*.mcpb` built for your checkout, and restart Claude Desktop. If tools time out on first use, run `uv run garmin-owl-sync` once from a terminal so the cache is warm before asking Claude.

## Privacy and safety

- Garmin access is read-only, and the server communicates over local stdio.
- Tokens stay in `~/.garminconnect`; normalized data stays in the local SQLite cache.
- Output excludes credentials, account identifiers, raw GPS coordinates, and private cycle logs.
- Health summaries are informational, not medical advice.

Review your MCP client's own privacy and data-retention settings before sending health information to any model.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy src tests
```

For a live, developer-only connection check:

```bash
uv run garmin-owl-smoke --activity-id ACTIVITY_ID
```

The smoke command reports only the failing Garmin read and exception class; it never prints raw responses or sensitive values.

### Build the Claude extension

```bash
./scripts/build-extension.sh
```

This validates `manifest.json` and creates a versioned `.mcpb` file in `dist/`. The bundle includes the Python project so it can run from Claude Desktop's extension directory; it never includes secrets or the local health database.

## Limitations and removal

Garmin Connect is a private API, and metric availability varies by device and account. If Garmin changes an endpoint, authentication or individual reads may temporarily fail.

To remove `garmin-owl`, uninstall the extension in Claude Desktop, delete the repository, and optionally delete the SQLite cache. Remove `~/.garminconnect` only if you also want to discard locally stored Garmin tokens used by other tools.
