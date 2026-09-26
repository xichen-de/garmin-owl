# garmin-owl

`garmin-owl` lets Claude Desktop (or another MCP client) answer questions about your Garmin health and training data: sleep, HRV, recovery, activities, training load, and more. It runs entirely on your computer, can only *read* from Garmin Connect, and never uploads a separate copy of your data anywhere.

It works on macOS and Ubuntu Linux and uses the unofficial [`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect) client, so a change on Garmin's side can occasionally break a read until `garmin-owl` is updated.

> Want to change the code or build a release? See [CONTRIBUTING.md](CONTRIBUTING.md).

## What you need

- macOS or Ubuntu Linux
- [Claude Desktop](https://claude.ai/download) with Extensions support, or another MCP client
- A Garmin Connect account with data from a Garmin device
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) and Git. `uv` installs Python 3.12+ for you, so you never need to set up or activate an environment.

## Setup

### 1. Sign in to Garmin (once)

In a terminal, run this as the same OS user that runs Claude Desktop:

```bash
git clone https://github.com/xichen-de/garmin-owl.git
cd garmin-owl
uv sync --locked
uv run garmin-owl-auth
```

You'll be asked for your Garmin email, password, and MFA code if your account uses one. Your password is **not** stored: only a reusable session token is saved, to `~/.garminconnect` (or to the directory in the `GARMINTOKENS` environment variable, if you set it). Treat that directory like a password.

Re-run `uv run garmin-owl-auth` any time to check your sign-in. It only asks for credentials again if the saved session no longer works.

<details>
<summary><b>Why does this ask for my Garmin password?</b></summary>

Garmin has no public API for personal data, so the unofficial client signs in the way the Garmin Connect website does. Your credentials are used once, in the terminal, to obtain session tokens. They never enter an MCP conversation, and Claude never sees them.

</details>

### 2. Install the Claude Desktop extension

1. Download the latest `.mcpb` file from [Releases](https://github.com/xichen-de/garmin-owl/releases).
2. Open Claude Desktop → **Settings** → **Extensions** and drag the `.mcpb` file in.
3. Enable the extension, restart Claude Desktop if prompted, and start a new chat.

The extension reuses the session saved in step 1. It contains the program only, never credentials, tokens, or health data.

<details>
<summary><b>Using a different MCP client</b></summary>

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

</details>

### 3. Try it

Ask Claude things like:

- "Summarize my recovery today."
- "How did I sleep last night?"
- "Compare my last two runs."
- "Show my 28-day recovery trend."
- "What did my training week look like around 3 March 2025?"
- "How has my weight changed over the last year?"
- "How does my cycle day line up with my recovery?"

## What it can tell you

Claude chooses among 19 read-only tools. None of them can change anything in your Garmin account.

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
| `get_activities` | Activity summaries over up to 366 days (defaults to the last 14), at most 100 results |
| `get_recent_activities` | Activities from the last 1–90 days, optionally filtered by type (for example `running`) |
| `get_activity` | One activity's laps, training effect, and HR/power zones |
| `compare_activities` | Side-by-side metrics for 2–10 activities |
| `get_training_week` | Mon–Sun totals and zone time, with per-metric coverage |
| `get_training_load` | Acute/chronic load, ratio/status, load focus/targets, VO2 max, endurance, hill, acclimation |
| `get_training_zones` | Configured HR and cycling-power zone thresholds |
| `get_running_tolerance` | Running distance, impact load, tolerance, and feedback over 1–90 days |

**Body, combined, and trends**

| Tool | Answers |
| --- | --- |
| `get_body_composition` | Weight and related measurements over up to 366 days (defaults to the last 30) |
| `get_recovery` | Sleep, HRV, Body Battery, stress, RHR, and readiness for one day |
| `get_recovery_trend` | Sleep score, sleep HR, skin-temperature deviation, HRV, RHR, readiness, and Body Battery over the last 7, 14, or 28 days |
| `get_training_context` | One day's recovery plus the 7 days of training before it |

### Dates and range limits

You can ask about any past date. The only limit on history is how much Garmin still returns for your account and device. Leave the date out and the tools use today. What *is* limited is how much one request covers:

| Coverage per request | Tools |
| --- | --- |
| One day (any date) | The "one day at a time" tools, `get_recovery`, `get_training_load` |
| Up to 366 days | `get_activities`, `get_body_composition` |
| Up to 90 days | `get_recent_activities` (ending today), `get_running_tolerance` (ending on any date) |
| Fixed window | `get_recovery_trend`: last 7, 14, or 28 days, ending today<br>`get_training_context`: the 7 days ending on the chosen date<br>`get_training_week`: the Mon–Sun week containing the chosen date |

`get_activity` and `compare_activities` take activity IDs, not dates, and `get_training_zones` reads your current settings. Activity lists return at most 100 activities per request.

You don't need to know these limits: describe the period in plain language and Claude picks the dates. For longer periods, such as several years of activities, Claude can split the question into several requests. A request that is too long returns an error that states the limit.

### How to read the answers

- **Garmin's numbers and `garmin-owl`'s calculations are kept apart.** Anything calculated (such as "12% above your recent average") states its baseline dates, how many days it used, and its formula.
- **Missing stays missing.** Nothing is guessed, and an absent value is never counted as zero.
- **Totals say how complete they are**, for example "distance covers 3 of 4 activities".
- **Gaps are explained** in an `availability` list: Garmin had no data, the metric is unsupported on your device, or the read failed or was rate-limited.
- `get_cycle` deliberately leaves out notes, symptoms, moods, sexual activity, and raw daily logs.

## Faster answers with the local cache (optional)

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

Set `GARMIN_OWL_DB` to use a different file. If you used a version before 0.2.1 on Linux, your old cache is at the macOS-style path above. Point `GARMIN_OWL_DB` at it to keep it, or let the new cache fill up by itself.

<details>
<summary><b>When is cached data refreshed?</b></summary>

Watches and scales upload late, so a day counts as final only from **noon the next day**. Data fetched after that point is kept for good. Data fetched earlier, while the day could still change, is reused for at most 20 minutes and then fetched again. Today's numbers therefore stay current, and a half-synced day never gets stuck in the cache.

</details>

## Updating

1. Download the new `.mcpb` from [Releases](https://github.com/xichen-de/garmin-owl/releases) and drag it into **Settings** → **Extensions** again. It replaces the old version.
2. Update your terminal copy, which is used for `garmin-owl-auth`, sync, and the cache commands:

   ```bash
   cd garmin-owl
   git pull
   uv sync --locked
   ```

The cache upgrades itself when needed. Your saved sign-in carries over.

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
| "rate limit reached" | Too many Garmin requests in a short time | Wait a few minutes. Sync smaller ranges. |
| "Garmin Connect is unavailable" | A network problem or Garmin outage | Try again later |
| "unexpected response shape" | Garmin changed a private endpoint | [Open an issue](https://github.com/xichen-de/garmin-owl/issues) naming the tool (never paste your Garmin data) |
| "no data for this request" | That metric isn't recorded for that date or device | Expected for unsupported metrics |
| "date range cannot exceed" / "days must be between" | The request was longer than the tool allows | Ask for a shorter period, or several periods |
| "Unsupported garmin-owl cache schema" | The cache was created by a newer version | Update `garmin-owl`, or run `uv run garmin-owl-cache-clear` |

- **Extension doesn't appear:** make sure you installed a `.mcpb` from Releases (or one built from the same version as your checkout), then restart Claude Desktop.
- **Tools time out the first time:** run `uv run garmin-owl-sync` once to warm the cache.
- **Sign-in works in the terminal but not in Claude:** run `garmin-owl-auth` as the same OS user that runs Claude Desktop, and if you set `GARMINTOKENS`, make sure Claude Desktop sees it too.

## Privacy and safety

- Everything runs locally and talks to your MCP client only over local stdio. There is no network listener, telemetry, or remote database.
- Garmin access is read-only. `garmin-owl` has no tool that can change your account.
- Tokens stay in `~/.garminconnect`. Only normalized numbers are stored in the local cache, never raw Garmin responses.
- Answers exclude credentials, account identifiers, raw GPS coordinates, and private cycle logs.
- Health summaries are informational, **not medical advice**.

What you ask Claude, and the answers it gets from `garmin-owl`, are handled under your MCP client's privacy and data-retention settings, so review those before sharing health information with any model.

## Limitations and removal

Garmin Connect's API is private, and which metrics exist depends on your device and account. When Garmin changes an endpoint, sign-in or individual reads can fail until `garmin-owl` is updated.

To remove `garmin-owl`:

1. Uninstall the extension in Claude Desktop.
2. Delete your `garmin-owl` folder.
3. Optionally delete the cache file listed above.
4. Delete `~/.garminconnect` only if no other Garmin tool uses those tokens.

## Contributing

Bug reports and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) explains the project layout, design rules, tests, and release process.
