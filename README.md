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

You'll be prompted in your terminal for your Garmin email, password, and MFA code if your account uses one. Only a reusable session token is saved, to `~/.garminconnect`. The MCP server never asks the model for any of these.

Re-run `uv run garmin-owl-auth` any time to check your saved tokens — it only re-authenticates if they've expired.

<details>
<summary><b>Why does this ask for my Garmin password?</b></summary>

Garmin has no public OAuth login for personal Garmin Connect data — the "Sign in with Garmin" redirect flow you'd get from Google or GitHub doesn't exist for individual accounts. Garmin's only OAuth API (the Connect Developer Program) is gated behind a business partnership agreement, not available to a personal script.

So `garmin-owl`, like every other open-source tool that reads personal Garmin data, authenticates the way the official Garmin Connect app does internally: your email and password go directly to Garmin's own login endpoint (`sso.garmin.com`) in exchange for session tokens, and only those tokens are stored locally.

Your credentials are typed once in your own terminal, held in memory for that one login call, and never logged, written to disk, or sent anywhere other than Garmin's servers. In particular, they never reach the MCP client or the model.

</details>

## Install in Claude Desktop

1. Authenticate once with `uv run garmin-owl-auth`.
2. Open Claude Desktop → **Settings** → **Extensions**.
3. Drag the latest `garmin-owl-*.mcpb` file from `dist/` into the Extensions window.
4. Enable the extension and restart Claude Desktop if prompted.

Then try:

- "Summarize my recovery today."
- "How did I sleep last night?"
- "Compare my last two activities."
- "Show my 28-day recovery trend."
- "How does my cycle day line up with my recovery?"

The bundle contains the project source but no credentials, tokens, or health data.

## Available tools

19 read-only tools. There are no write tools.

**One day at a time**

| Tool | Answers |
| --- | --- |
| `get_daily_summary` | Steps, activity/sedentary time, goals, calories, HR, stress, respiration, SpO2, Body Battery |
| `get_sleep` | Sleep score/need, stages, timing, sleeping HR/stress, respiration, SpO2, Body Battery change, skin-temperature deviation |
| `get_hrv` | HRV status, nightly and weekly averages |
| `get_body_battery` | Charged, drained, and start/end/highest/lowest levels |
| `get_stress` | Average and max stress, plus durations by intensity band |
| `get_training_readiness` | Garmin's readiness score, components, and factor feedback |
| `get_body_composition` | Weight and related measurements over a date range |
| `get_cycle` | Cycle phase, day, and Garmin predictions |

**Activities and training**

| Tool | Answers |
| --- | --- |
| `get_activities` | Activities in a date range, enriched for walking, cycling, and cardio (defaults to the last 14 days) |
| `get_recent_activities` | Activities from the last N days, optionally filtered by type |
| `get_activity` | One activity's laps, training effect, and HR/power zones |
| `compare_activities` | Side-by-side metrics for 2–10 activities |
| `get_training_week` | Mon–Sun totals and zone time, with per-metric coverage |
| `get_training_load` | Acute/chronic load, ratio/status, load focus/targets, VO2 max, endurance, hill, acclimation |
| `get_training_zones` | Configured HR and cycling-power zone thresholds |
| `get_running_tolerance` | Garmin running distance, impact load, tolerance, and feedback over 1–90 days |

**Combined and trends**

| Tool | Answers |
| --- | --- |
| `get_recovery` | Sleep, HRV, Body Battery, stress, RHR, and readiness for one day |
| `get_recovery_trend` | Sleep HR, skin-temperature deviation, HRV, RHR, readiness, and Body Battery across 7, 14, or 28 days |
| `get_training_context` | Recovery plus the requested date's preceding training |

### How results are reported

- **Garmin values and `garmin-owl` calculations stay distinguishable.** Derived comparisons state their baseline dates, sample count, and formula.
- **Missing metrics stay missing.** Nothing is guessed, and nothing absent is summed as zero.
- **Totals disclose their coverage** — how many activities actually reported the metric.
- **An `availability` list explains every gap**, distinguishing "Garmin had no data" from "unsupported on this device" from "the read failed or was rate-limited."

`get_cycle` intentionally excludes notes, symptoms, moods, sexual activity, and raw daily logs.

## Sync and local cache

Optional, but makes later requests faster:

```bash
uv run garmin-owl-sync            # last 7 days
uv run garmin-owl-sync --days 30
```

This loads daily summaries, sleep, HRV, training readiness, and activity summaries, fetching only what's missing or stale. Body Battery, stress, activity details, and cycle data are fetched on demand instead.

```bash
uv run garmin-owl-cache-info      # inspect
uv run garmin-owl-cache-clear     # clear (leaves auth tokens alone)
```

The cache lives at `~/Library/Application Support/garmin-owl/garmin.sqlite`. Set `GARMIN_OWL_DB` to move it.

<details>
<summary><b>How the cache decides something is stale</b></summary>

Watches and scales upload late, so a calendar day is treated as settled only at **noon the following day**.

A record is trusted indefinitely once it was *fetched* after its day settled. A record captured while the day was still synchronizing is reused for at most 20 minutes, then re-fetched — so a partially synced day never becomes permanently authoritative. The same rule covers date ranges and cached activity details.

</details>

## Troubleshooting

`garmin-owl` never retries automatically and never surfaces raw Garmin responses, so error messages are short.

| Message contains | Meaning | Fix |
| --- | --- | --- |
| "No local Garmin tokens found" | Not authenticated yet, or `~/.garminconnect` was deleted | Run `uv run garmin-owl-auth` |
| "authentication expired or was rejected" | Garmin logged the session out (e.g. after a password change) | Run `uv run garmin-owl-auth` again |
| "rate limit reached" | Too many Garmin requests too quickly | Wait a few minutes; running `garmin-owl-sync` less often also helps |
| "Garmin Connect is unavailable" | A transient network or Garmin outage | Try again later |
| "unexpected response shape" | Garmin changed a private endpoint's fields | [Open an issue](https://github.com/xichen-de/garmin-owl/issues) with the tool name (never paste your Garmin data) |
| "no data for this request" | That metric isn't recorded for that date or device | Expected for unsupported metrics; not an error to fix |

If the extension doesn't appear after installing, confirm the `.mcpb` matches the one built for your checkout and restart Claude Desktop. If tools time out on first use, run `uv run garmin-owl-sync` once so the cache is warm.

## Privacy and safety

- Garmin access is read-only, over local stdio.
- Tokens stay in `~/.garminconnect`; normalized data stays in the local SQLite cache.
- Output excludes credentials, account identifiers, raw GPS coordinates, and private cycle logs.
- Health summaries are informational, **not medical advice**.

Review your MCP client's own privacy and data-retention settings before sending health information to any model.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy src tests
```

Build the Claude Desktop extension:

```bash
./scripts/build-extension.sh
```

This validates `manifest.json` and writes a versioned `.mcpb` to `dist/`. It never includes secrets or the local health database.

<details>
<summary><b>Investigating a missing or unlabeled metric</b></summary>

All three commands below are redacted by design: they report structure, key names, and exception classes only — never response values.

Check a live connection:

```bash
uv run garmin-owl-smoke --activity-id ACTIVITY_ID
```

Garmin returns `trainingStatus` as an unlabeled numeric code. `garmin-owl` reports it as `training_status_code` and does not guess what a code means; `training_status` is populated only when Garmin also sends wording. To see whether your account's response carries a label key:

```bash
uv run python -m garmin_owl.diagnostic --training-status 2026-08-31
```

To check whether a metric is reachable at all through reads `garmin-owl` is already permitted to make, scan those responses for matching key names. This goes through the same allow-list the server uses, so it cannot look anywhere the server itself cannot:

```bash
uv run python -m garmin_owl.diagnostic --find-keys 2026-08-31 temp
```

</details>

## Limitations and removal

Garmin Connect is a private API, and metric availability varies by device and account. If Garmin changes an endpoint, authentication or individual reads may temporarily fail.

To remove `garmin-owl`, uninstall the extension in Claude Desktop and delete the repository, and optionally the SQLite cache. Remove `~/.garminconnect` only if you also want to discard Garmin tokens used by other tools.
