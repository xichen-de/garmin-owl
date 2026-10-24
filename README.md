# garmin-owl

Ask Claude (or ChatGPT) about your Garmin data: sleep, HRV, recovery, workouts, training load, and long-term trends.

- **Private:** runs on your computer. There is no server or account in between.
- **Read-only:** it can't change anything in your Garmin account.
- **Honest:** when Garmin has no value, it says so instead of guessing.

Works on macOS and Ubuntu Linux.

## What you need

- [Claude Desktop](https://claude.ai/download) (or [ChatGPT Desktop](docs/chatgpt.md), or [another MCP app](docs/reference.md#other-mcp-apps))
- A Garmin Connect account
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) and Git

## Setup

### 1. Sign in to Garmin

Run this once in a terminal:

```bash
git clone https://github.com/xichen-de/garmin-owl.git
cd garmin-owl
uv sync --locked
uv run garmin-owl-auth
```

Enter your Garmin email, password and MFA code. Your password isn't saved, only a session token in `~/.garminconnect`. Keep that folder private.

### 2. Install the extension

1. Download the latest `.mcpb` file from [Releases](https://github.com/xichen-de/garmin-owl/releases).
2. Drag it into Claude Desktop → **Settings** → **Extensions**, then enable it.
3. Start a new chat.

Using ChatGPT? Follow [docs/chatgpt.md](docs/chatgpt.md) instead.

### 3. Ask

- "Summarize my recovery today."
- "How did I sleep last night?"
- "Compare my last two runs."
- "How have my resting HR and HRV changed month by month over the last year?"
- "Show my VO2 max since I got my watch."

## What it can answer

- **Any day:** sleep, HRV, resting HR, stress, Body Battery, training readiness, cycle phase
- **Workouts:** activity lists, details with your own notes and effort ratings, comparisons, weekly totals
- **Training:** load and status, zones, running tolerance, VO2 max
- **Long-term:** daily history or monthly summaries over up to three years, and when your data starts

See the [full tool list and limits](docs/reference.md#tools).

## Add your own context (optional)

Trends often depend on things Garmin doesn't know about. Write them in `~/.garmin-owl/events.toml`:

```toml
[[event]]
date = 2026-03-01
end_date = 2026-06-30    # optional
label = "Started new medication"

[[event]]
date = 2026-01-12
label = "Flu"
```

Long-range answers include the events from the period they cover. garmin-owl only reads this file.

## Updating

Download the new `.mcpb` from [Releases](https://github.com/xichen-de/garmin-owl/releases) and drag it in again. Then run `git pull` in your `garmin-owl` folder. Your sign-in and cached data carry over.

## If something goes wrong

Check sign-in and the main reads (this shows pass/fail only, never your data):

```bash
uv run garmin-owl-smoke
```

- **"authentication expired" or "No local Garmin tokens":** run `uv run garmin-owl-auth` again.
- **"rate limit reached":** wait a few minutes and ask again.
- **Extension not showing:** restart Claude Desktop.

Anything else: see [Troubleshooting](docs/reference.md#troubleshooting).

## Privacy

- Your data goes only from Garmin to your computer and into your chat. There is no telemetry and no remote database.
- garmin-owl keeps a local cache so each day is fetched only once ([details](docs/reference.md#local-cache)).
- Whatever reaches your chat is handled under your Claude or ChatGPT privacy settings.
- This is not medical advice.

garmin-owl uses the unofficial [`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect) client, so a change on Garmin's side can occasionally break a read until garmin-owl is updated.

## Uninstall

Remove the extension in Claude Desktop, then delete your `garmin-owl` folder. To also remove your data, delete the cache (`~/Library/Application Support/garmin-owl` on macOS, `~/.local/share/garmin-owl` on Linux) and `~/.garmin-owl`. Delete `~/.garminconnect` only if no other Garmin tool uses it.

---

Developing garmin-owl? See [CONTRIBUTING.md](CONTRIBUTING.md).
