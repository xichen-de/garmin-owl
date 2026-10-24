# garmin-owl reference

Details behind the [README](../README.md): every tool, date limits, how answers are shaped, the local cache, and troubleshooting.

- [Tools](#tools)
- [Dates and range limits](#dates-and-range-limits)
- [How to read the answers](#how-to-read-the-answers)
- [Your own events](#your-own-events-optional)
- [Local cache](#local-cache)
- [Troubleshooting](#troubleshooting)
- [Other MCP apps](#other-mcp-apps)
- [Why sign-in needs your password](#why-does-sign-in-need-my-garmin-password)
- [Updating in detail](#updating-in-detail)
- [Privacy in detail](#privacy-in-detail)

## Tools

Your assistant chooses among 24 read-only tools. None of them can change anything in your Garmin account.

**One day at a time**

| Tool | Answers |
| --- | --- |
| `get_daily_summary` | Steps, activity/sedentary time, goals, calories, HR, stress, respiration, SpO2, Body Battery |
| `get_sleep` | Sleep score/need, stages, timing, sleeping HR/stress, respiration, SpO2, Body Battery change, skin-temperature deviation |
| `get_hrv` | HRV status, nightly and weekly averages |
| `get_body_battery` | Charged, drained, and start/end/highest/lowest levels |
| `get_stress` | Average and max stress, plus durations by intensity band |
| `get_training_readiness` | Garmin's readiness score, components, and factor feedback |
| `get_cycle` | Cycle phase, day, and Garmin predictions |

**Activities and training**

| Tool | Answers |
| --- | --- |
| `get_activities` | Activity summaries over up to 366 days (defaults to the last 14), filtered by type or minimum duration, in full or compact form, paged with `offset`/`next_offset` |
| `get_recent_activities` | Activities from the last 1–90 days, optionally filtered by type (for example `running`) |
| `get_activity` | One activity's laps, training effect, HR/power zones, the HR-zone floors in effect, and your [notes and ratings](#your-own-training-notes) |
| `get_activities_detail` | The same details for up to 50 activities in one request (compact by default) |
| `compare_activities` | Side-by-side metrics for 2–10 activities |
| `get_training_week` | Mon–Sun totals and zone time, with per-metric coverage |
| `get_training_load` | Acute/chronic load, ratio/status, load focus/targets, VO2 max, endurance, hill, acclimation |
| `get_training_zones` | Today's configured HR and cycling-power zone thresholds (Garmin keeps no history; see `hr_zone_floors_bpm` on activities) |
| `get_running_tolerance` | Running distance, impact load, tolerance, and feedback over 1–90 days |

**Body, combined, and trends**

| Tool | Answers |
| --- | --- |
| `get_body_composition` | Weight and related measurements over up to 366 days (defaults to the last 30) |
| `get_recovery` | Sleep, HRV, Body Battery, stress, RHR, and readiness for one day |
| `get_recovery_trend` | Sleep score, sleep HR, skin-temperature deviation, HRV, RHR, readiness, and Body Battery over the last 1-28 days |
| `get_training_context` | One day's recovery plus the 7 days of training before it |

**Long-range history**

| Tool | Answers |
| --- | --- |
| `get_data_coverage` | First and last date with data per family (daily/resting HR, HRV, sleep, VO2 max, readiness, activities), activity count, and your devices |
| `get_daily_series` | One compact row per day for up to 366 days: resting HR, HRV, sleep score/HR/duration, respiration, skin temperature, Body Battery, stress, and optionally readiness and Body Battery at wake |
| `get_period_summary` | Per-month (up to 3 years) or per-week (up to 1 year) mean, SD, min, max and day count for the same metrics |
| `get_vo2max_history` | Garmin's VO2 max estimates and fitness age over up to 3 years |

These read Garmin's range endpoints in the largest chunks Garmin accepts and cache every day, so a year of daily metrics costs about 40 Garmin requests the first time and none afterwards. "Monthly resting HR and HRV for the last 15 months" is one `get_period_summary` call (4 Garmin requests, about 4k characters), where it used to take about 130 single-day calls. Readiness and Body Battery at wake have no range read in Garmin, so they cost one request per day and are only read when asked for (at most 60 per call; ask again to continue).

### Dates and range limits

You can ask about any past date. The only limit on history is how much Garmin still returns for your account and device. Leave the date out and the tools use today. What *is* limited is how much one request covers:

| Coverage per request | Tools |
| --- | --- |
| One day (any date) | The "one day at a time" tools, `get_recovery`, `get_training_load` |
| Up to 3 years (1096 days) | `get_period_summary` by month, `get_vo2max_history` |
| Up to 366 days | `get_activities`, `get_body_composition`, `get_daily_series`, `get_period_summary` by week |
| Up to 90 days | `get_recent_activities` (ending today), `get_running_tolerance` (ending on any date) |
| Fixed window | `get_recovery_trend`: last 1-28 days, ending today<br>`get_training_context`: the 7 days ending on the chosen date<br>`get_training_week`: the Mon–Sun week containing the chosen date |

`get_activity`, `get_activities_detail` and `compare_activities` take activity IDs, not dates; `get_training_zones` and `get_data_coverage` take none. Activity lists return at most 100 activities per request and stop early rather than overflow the client; `truncated` says when more matched, and `next_offset` fetches the next page.

You don't need to know these limits: describe the period in plain language and Claude picks the dates. For longer periods, such as several years of activities, Claude can split the question into several requests. A request that is too long returns an error that states the limit.

Every timestamp (sleep start/end, readiness, activity and lap starts, weigh-ins, time-series
readings) is ISO 8601 with an explicit UTC offset, such as `2026-10-09T23:18:16+02:00`. It is
built from Garmin's true GMT value. The offset is the one Garmin recorded for your device at that
moment, or your computer's time zone when Garmin gives only UTC, so daylight-saving changes are
handled per timestamp. Dates (`YYYY-MM-DD`) are Garmin calendar days.

### How to read the answers

- **Garmin's numbers and `garmin-owl`'s calculations are kept apart.** Anything calculated (such as "12% above your recent average") states its baseline dates, how many days it used, and its formula.
- **Missing stays missing.** Nothing is guessed, and an absent value is never counted as zero. Garmin's `0.0` for something an activity didn't measure (an indoor workout's distance, a ride without a power meter) is left out too.
- **Totals say how complete they are**, for example "distance covers 3 of 4 activities".
- **Gaps are explained** in an `availability` list: Garmin had no data, the metric is unsupported on your device, or the read failed or was rate-limited. This includes a day with no data at all for sleep, HRV, readiness, stress, or Body Battery.
- **Units are in the field name** (`_m`, `_seconds`, `_hours`, `_percent`). Recovery time is in hours everywhere, and time series are always called `timeseries`.
- `get_cycle` deliberately leaves out notes, symptoms, moods, sexual activity, and raw daily logs.

### Your own training notes

Add notes to an activity's description in Garmin Connect, such as exercises, weights, or how it felt, and the assistant reads them alongside Garmin's numbers. `get_activity` and `get_activities_detail` return them exactly as you typed them, together with any perceived effort and "How did you feel?" ratings you gave the activity. Activity lists only flag that a note exists (`has_description`).

### Your own events (optional)

Long-range trends often depend on context Garmin doesn't have, such as starting a medication, an illness, travel, or a training break. Write them in `~/.garmin-owl/events.toml` (or point `GARMIN_OWL_EVENTS` at another file):

```toml
[[event]]
date = 2026-03-01
end_date = 2026-06-30        # optional, inclusive
category = "medication"      # optional
label = "Started beta blocker"

[[event]]
date = 2026-01-12
label = "Flu"
```

`get_daily_series`, `get_period_summary` and `get_vo2max_history` attach the events that overlap the requested period. `garmin-owl` only reads this file: it never writes it and never sends it anywhere except to your local MCP client with those answers. Invalid entries are skipped and named in `availability`.

### Exporting a series

`get_daily_series` with `output` set to `csv` or `json` writes the rows to a new file in the `exports` folder next to the [cache](#local-cache) and returns the file's path instead of the rows. The file name is chosen by `garmin-owl`; a tool call cannot write anywhere else.

## Local cache

`garmin-owl` keeps what it reads in a small local database, so each day is fetched from Garmin only once. That happens automatically, but you can pre-load history so your first questions are quick:

```bash
uv run garmin-owl-sync                                       # last 7 days
uv run garmin-owl-sync --days 30                             # last 30 days (max 366)
uv run garmin-owl-sync --start 2025-01-01 --end 2025-12-31   # any past window
```

- A single run covers at most **366 days**. `--days` counts back from today. `--start` can be any past date, and `--end` defaults to today.
- To load more than a year, run one window per year. Days already cached are skipped.
- `--refresh-today` or `--refresh-date YYYY-MM-DD` re-fetches a day even if it's cached.
- Sync pre-loads daily summaries, sleep, HRV, training readiness, and activity lists. Body Battery, stress, activity details, training load, body composition, and cycle data are fetched the first time you ask, then cached.

Inspect or clear the cache (clearing never touches your Garmin sign-in):

```bash
uv run garmin-owl-cache-info
uv run garmin-owl-cache-clear
```

The cache is stored at:

- **macOS:** `~/Library/Application Support/garmin-owl/garmin.sqlite`
- **Ubuntu/Linux:** `~/.local/share/garmin-owl/garmin.sqlite`, or under `$XDG_DATA_HOME` when that is set to an absolute path

The cache is bound to an account fingerprint checked against Garmin at startup. Use a separate
`GARMIN_OWL_DB` for each Garmin account; opening another account’s cache is rejected. Existing
caches without an account fingerprint must be explicitly cleared with `garmin-owl-cache-clear`
before reuse, or preserved by choosing a new database file. Clearing data keeps an existing
account binding. Account identifiers and credentials are not stored in the cache.

Successful activity and weigh-in range refreshes remove records deleted in Garmin from that
range. Failed or malformed responses leave the cache unchanged.

Set `GARMIN_OWL_DB` to use a different file. If you used a version before 0.2.1 on Linux, your old cache is at the macOS-style path above. Point `GARMIN_OWL_DB` at it to keep it, or let the new cache fill up by itself.

<details>
<summary><b>When is cached data refreshed?</b></summary>

Watches and scales upload late, so a day counts as final only from **noon the next day**. Data fetched after that point is kept for good. Data fetched earlier, while the day could still change, is reused for at most 20 minutes and then fetched again. Today's numbers therefore stay current, and a half-synced day never gets stuck in the cache.

Activity notes and ratings can be edited at any time, so if you change them on an older activity, ask the assistant to refresh that activity.

</details>

## Troubleshooting

First, check that sign-in and the main reads work. The output shows only pass/fail per read, never your data:

```bash
uv run garmin-owl-smoke
```

`garmin-owl` never retries automatically and never shows raw Garmin responses, so error messages are short:

| Message contains | Meaning | Fix |
| --- | --- | --- |
| "No local Garmin tokens found" | Not signed in yet, or `~/.garminconnect` was deleted | Run `uv run garmin-owl-auth` |
| "authentication expired or was rejected" | Garmin ended the session, for example after a password change | Run `uv run garmin-owl-auth` again |
| "rate limit reached (HTTP 429)" | Too many Garmin requests in a short time | Wait a few minutes. Sync smaller ranges. |
| "Could not reach Garmin Connect" | A network problem or timeout | Check the connection, then try again later |
| "server error (HTTP 5xx)" | Garmin Connect itself is failing | Try again later |
| "rejected this request as invalid or too large (HTTP 4xx)" | Garmin refused this exact request | Ask for a shorter period; retrying unchanged won't help |
| "unexpected response shape" / "unusable response" | Garmin changed a private endpoint | [Open an issue](https://github.com/xichen-de/garmin-owl/issues) naming the tool (never paste your Garmin data) |
| "no data for this request" | That metric isn't recorded for that date or device | Expected for unsupported metrics |
| "date range cannot exceed" / "days must be between" | The request was longer than the tool allows | Ask for a shorter period, or several periods |
| "Cache belongs to another Garmin account" | The cache was created while signed in to a different account | Use a separate `GARMIN_OWL_DB` for each account |
| "upgraded … by a newer garmin-owl" | A newer version changed the cache while this one was running | Restart the app |
| "local garmin-owl cache could not be read or written" | SQLite error, often another process holding the file | Close other garmin-owl processes; if it persists, run `uv run garmin-owl-cache-clear` |
| "unexpected internal error (…)" | A bug; details are withheld to protect your data | Retry once, then restart and report the tool name and error type |

The cache repairs itself after updates. Rows stored in an older format are re-read from Garmin, rows cached before account binding existed are discarded and re-read, and a cache written by a newer version is renamed to `garmin.sqlite.schema-N` and replaced with a fresh one.

- **Extension doesn't appear:** make sure you installed a `.mcpb` from Releases (or one built from the same version as your checkout), then restart Claude Desktop.
- **Tools time out the first time:** run `uv run garmin-owl-sync` once to warm the cache.
- **Sign-in works in the terminal but not in Claude or ChatGPT:** run `garmin-owl-auth` as the same OS user that runs the desktop app, and if you set `GARMINTOKENS`, make sure the app sees it too.
- **ChatGPT Desktop problems:** see [ChatGPT troubleshooting](chatgpt.md#chatgpt-troubleshooting).

## Other MCP apps

For clients that accept `mcpServers` JSON, add the entry below. Replace the two placeholder paths with the output of `command -v uv` and of `pwd` run inside your `garmin-owl` folder. Where the config file lives depends on your client.

```json
{
  "mcpServers": {
    "garmin-owl": {
      "command": "/absolute/path/to/uv",
      "args": ["--directory", "/absolute/path/to/garmin-owl", "run", "garmin-owl"]
    }
  }
}
```

Use absolute paths, because desktop apps often don't inherit your terminal's `PATH`. Running `uv run garmin-owl` by hand just waits silently for an MCP client, so it isn't an interactive program.

## Why does sign-in need my Garmin password?

Garmin has no public API for personal data, so the unofficial client signs in the way the Garmin Connect website does. Your credentials are used once, in the terminal, to obtain session tokens. They never enter an MCP conversation, and Claude never sees them.

Re-run `uv run garmin-owl-auth` any time to check your sign-in. It only asks for credentials again if the saved session no longer works. Run it as the same OS user that runs your desktop app; if you set `GARMINTOKENS`, the token folder is that directory instead of `~/.garminconnect`.

## Updating in detail

1. Download the new `.mcpb` from [Releases](https://github.com/xichen-de/garmin-owl/releases) and drag it into **Settings** → **Extensions** again. It replaces the old version. For ChatGPT Desktop, follow [Updating the ChatGPT plugin](chatgpt.md#updating-the-chatgpt-plugin) after step 2 instead.
2. Update your terminal copy, which is used for `garmin-owl-auth`, sync, and the cache commands:

   ```bash
   cd garmin-owl
   git pull
   uv sync --locked
   ```

The cache upgrades itself when needed. Your saved sign-in carries over.

## Privacy in detail

- Everything runs locally and talks to your MCP client only over local stdio. There is no network listener, telemetry, or remote database.
- Garmin access is read-only. `garmin-owl` has no tool that can change your account.
- Tokens stay in `~/.garminconnect`. Only normalized data, including your activity notes, is stored in the local cache, never raw Garmin responses.
- Your events file is read-only for `garmin-owl`, and exports are written only into its own `exports` folder with owner-only permissions.
- Device information excludes serial numbers, unit IDs and device nicknames.
- Answers exclude credentials, account identifiers, raw GPS coordinates, and private cycle logs.
- Health summaries are informational, **not medical advice**.

What you ask Claude, and the answers it gets from `garmin-owl`, are handled under your MCP client's privacy and data-retention settings, so review those before sharing health information with any model.
