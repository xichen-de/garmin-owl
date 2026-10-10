# Contributing to garmin-owl

This guide is for people who change `garmin-owl` itself. For installing and using it, see the [README](README.md).

## Development setup

You need macOS or Ubuntu, [`uv`](https://docs.astral.sh/uv/getting-started/installation/), and Git. Building the Claude Desktop extension also needs [Node.js LTS](https://nodejs.org/en/download) with `npx`.

```bash
git clone https://github.com/xichen-de/garmin-owl.git
cd garmin-owl
uv sync --extra dev
```

Run the same checks CI runs on Ubuntu and macOS, and make sure all of them pass before you open a pull request:

```bash
uv run pytest
uv run ruff check .
uv run mypy src tests
```

The test suite never contacts Garmin. For manual testing against your own account, sign in once with `uv run garmin-owl-auth`, then try the server with the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector uv run garmin-owl
```

Point `GARMIN_OWL_DB` at a scratch file while you experiment, so your real cache stays untouched:

```bash
GARMIN_OWL_DB=/tmp/owl-dev.sqlite npx @modelcontextprotocol/inspector uv run garmin-owl
```

## How the code is organized

A tool call flows from top to bottom through these layers:

```
server.py      MCP registration only: one thin function per tool, stdio transport
   │
tools.py       GarminTools: input validation, cache-or-fetch, combining reads, availability notices
   │
   ├── sync.py       SyncEngine: "is this cached and fresh? if not, fetch, normalize, store"
   ├── database.py   GarminDatabase: SQLite schema, migrations, freshness rules, typed get/put
   │
normalize.py   raw Garmin JSON → models, defensively (every key optional, alternate key names)
models.py      Pydantic output models; OwlModel.compact() drops empty fields for small responses
notices.py     shared AvailabilityNotice builders, used by both live reads and cache hits
   │
client.py      GarminDataClient: the explicit allow-list of Garmin reads and error translation
auth.py        token loading/refresh and the interactive login; the only code that sees credentials
```

Command-line entry points (see `[project.scripts]` in `pyproject.toml`):

| Command | Module | Purpose |
| --- | --- | --- |
| `garmin-owl` | `server.py` | The MCP server (stdio) |
| `garmin-owl-auth` | `auth.py` | Interactive sign-in |
| `garmin-owl-sync` | `sync.py` | Pre-load the cache |
| `garmin-owl-cache-info`, `garmin-owl-cache-clear` | `cache_cli.py` | Inspect or clear the cache |
| `garmin-owl-smoke` | `smoke.py` | Live pass/fail check of the core reads |
| `python -m garmin_owl.diagnostic` | `diagnostic.py` | Redacted response-shape probes (see below) |

## Design rules

These are the project's guarantees to users. A change that breaks one of them needs a very good reason and an explicit discussion in the pull request.

1. **Read-only by construction.** Garmin is reached only through the methods listed in `GarminReadAPI` and wrapped in `GarminDataClient`. Don't add generic request helpers or any method that writes. The `test_server` test pins the exact set of registered tools.
2. **No raw data at rest or in output.** Only explicit, normalized scalar fields go into models and the SQLite cache. Raw responses, credentials, account identifiers, GPS tracks, and cycle notes/symptoms never do.
3. **Missing is not zero.** An absent value stays `None`. Totals sum only the values that are present and report how many that was (see `_sum_present`).
4. **Explain every gap.** When a value is absent, add an `AvailabilityNotice` that says why: `missing_or_unsupported`, `retrieval_failed`, `retrieval_failed_rate_limited`, and so on. Anything `garmin-owl` calculates, rather than Garmin reporting it, gets a `derived_notice` with its formula.
5. **A cache hit answers exactly like a live read.** If a notice or field can't be stored, rebuild it on read from what is stored, using the same builder in `notices.py`. Tests compare the live and cached responses.
6. **Anchored to the requested date.** Never answer a question about a historical date with "latest" data. That's why the "most recent N activities" read isn't exposed.
7. **Errors are short and safe.** `client._safe_call` turns upstream exceptions into `GarminOwlError` subclasses with fixed messages and drops the original message (`from None`). There are no automatic retries.
8. **Bounded requests.** Every range has a maximum (366 days for ranges, 1–90 days for rolling windows, 100 activities, 48 time-series points). Invalid input raises `ValueError` with a message the model can act on.
9. **Local only.** stdio transport, no listener, no telemetry. Tool descriptions must never suggest medical advice.

## Testing

- Tests live in `tests/` and use fake Garmin objects, such as `FakeGarmin` in `tests/test_tools.py`, that record which upstream methods were called. Wrap a fake in the real client with `GarminDataClient(fake)`.
- `GarminTools(GarminDataClient(fake))` runs with **no cache**. Pass `GarminDatabase(tmp_path / "garmin.sqlite")` as the second argument to test cache behaviour.
- Assert on `fake.calls` whenever a change affects how often Garmin is read. Request counts are part of the behaviour.
- For each bug fix, add a regression test that fails without the fix.
- Never commit real Garmin responses. Build fixtures by hand, keeping only the keys the code reads.

## Common changes

### Adding a metric to an existing tool

1. Read the new key in the matching `normalize_*` function in `normalize.py`, with alternate key names if Garmin uses more than one.
2. Add the field to the model in `models.py`.
3. If the tool is cached, store the field too (see [Changing the cache schema](#changing-the-cache-schema)).
4. Add or extend a normalization test, and a cache round-trip test if the field is stored.

### Adding a tool

1. **Client:** if the tool needs a new Garmin read, add the method to `GarminReadAPI` and a wrapper to `GarminDataClient` that goes through `self._read(...)`.
2. **Normalize and model:** add a normalizer and a model that inherits `OwlModel`.
3. **Service:** add a method to `GarminTools` that validates input (`parse_date`, `parse_range`, bounds), uses the cache if the data is cacheable, and returns `.compact()`.
4. **Register:** add a thin function in `server.py`. Its docstring is the description the model sees: explain when to choose it over related tools, relevant limits, authentication, and cache behavior. Add `Annotated`/`Field` descriptions for every parameter and the shared read-only tool annotations. `tests/test_server.py` checks the exported MCP metadata without authenticating. Declare outputs as `Annotated[CallToolResult, YourModel]`; list results get an envelope model with `count` (and `truncated` when capped), like `ActivityList`, and return `_tool_result(...)` so the SDK validates against the model without restoring omitted fields as null. Reuse the models in `models.py`; keep missing metrics optional.
5. **Declare:** add the tool to the `tools` list in `manifest.json` and to the expected set in `tests/test_server.py`. If you created a new module, add it to `PACKAGE_FILES` in `scripts/build-chatgpt-plugin.py`.
6. **Document:** add a row to the tool tables and, if relevant, the date-limit table in `README.md`.

### Changing the cache schema

The schema lives in `database.py`.

- Add a new column both to the `CREATE TABLE` statement in `SCHEMA` (for new caches) and to `ADDED_COLUMNS` (so existing caches gain it through `ALTER TABLE` on startup).
- If rows cached by older versions would now be incomplete or change meaning (including a changed value format), bump `SCHEMA_VERSION` and add a step to `_migrate()` that marks those rows stale, like the `version < 5` and `version < 8` steps do. When a value can be corrected exactly (a unit or rounding fix), correct it in place, as the `version < 9` step does.
- The stored version is checked when the cache opens and again at the start of every transaction. An older file is migrated in place. A file written by a newer version is renamed to `garmin.sqlite.schema-N` and replaced, so never lower `SCHEMA_VERSION`.
- Cached single-row reads go through `_discard_unreadable`: a row that no longer fits its model is deleted and re-fetched instead of failing.
- Timestamps are produced only by `normalize.local_iso()`, which returns ISO 8601 with an explicit offset built from Garmin's GMT value. Never emit raw `*Local` values.

Freshness rules: a day is settled at `DAY_SETTLES_AT`, noon the next day. A row fetched after that is kept for good, and a row fetched before it is reused for `TODAY_TTL`, 20 minutes. Keep new cached resources on the same rule by going through `is_fresh` / `is_range_fresh`.

## Investigating Garmin response changes

When a metric is missing or a read fails with "unexpected response shape", use the redacted tools. They print key names, container types, and exception classes, never values, so their output can go into a GitHub issue:

```bash
uv run garmin-owl-smoke                                            # pass/fail for today's core reads
uv run python -m garmin_owl.diagnostic ACTIVITY_ID                 # shape of activity detail/zone reads
uv run python -m garmin_owl.diagnostic --training-status 2026-08-31
uv run python -m garmin_owl.diagnostic --find-keys 2026-08-31 temp
```

- `--training-status` reports whether Garmin sent any wording with the numeric `trainingStatus` code. `garmin-owl` reports the code as `training_status_code` and deliberately ships no code-to-label table.
- `--find-keys` searches the responses of reads that are already allow-listed for key names containing a substring. It goes through `GarminDataClient`, so it can't look anywhere the server can't.

## Building the extension

```bash
./scripts/build-extension.sh
```

The script checks that all version numbers match, validates `manifest.json` with the pinned `@anthropic-ai/mcpb` packager, and writes `dist/garmin-owl-<version>.mcpb`. `.mcpbignore` controls what goes into the bundle: tests, CI files, the ChatGPT adapter files, local databases, and anything token-like are excluded. The first build needs internet access.

## Building the ChatGPT Desktop plugin

```bash
uv run --locked python scripts/build-chatgpt-plugin.py
```

This writes a local marketplace to `dist/chatgpt-marketplace/`. The adapter is three files on top of the normal server:

| File | Role |
| --- | --- |
| `plugin.json` | Agent Plugins manifest plus OpenAI UI metadata under `extensions.com.openai` |
| `mcp.json` | Starts the stdio server via the launcher, with the venv in `${PLUGIN_DATA}/venv` |
| `scripts/launch-chatgpt.sh` | Finds `uv` without the shell's `PATH` and runs `uv run --locked garmin-owl` |

The script copies only the files in `PACKAGE_FILES` and rejects symlinked inputs, so tokens, caches, and other checkout state can never end up in the plugin. When you add a module under `src/garmin_owl/`, add it to `PACKAGE_FILES` too; `tests/test_chatgpt_plugin.py` fails until you do.

That test file also validates both manifests against the vendored official 1.0.0 schemas in `tests/schemas/`, checks clean staging, exercises the launcher's `uv` discovery, and starts the staged server over stdio without Garmin credentials. The startup test reuses the dev environment and imports the staged source, so it runs offline.

The schemas don't cover OpenAI's UI metadata or the marketplace runtime. When you change the adapter, also install the plugin in ChatGPT Desktop (see the README) and confirm that it appears in **Garmin Owl Local**, installs, and answers a question.

The release workflow doesn't publish a ChatGPT bundle; users build it from their checkout.

## Releasing

1. Set the new version in all six places:
   - `pyproject.toml` → `version`
   - `manifest.json` → `version`
   - `plugin.json` → `version`
   - `src/garmin_owl/__init__.py` → `__version__`
   - `src/garmin_owl/server.py` → `version=` in `MCPServer(...)`
   - `uv.lock`: run `uv lock` after editing `pyproject.toml`
2. Confirm that they match:

   ```bash
   uv run python scripts/check-release-version.py vX.Y.Z
   ```

3. Merge to `main`, then tag and push:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

The **Release** workflow runs the full checks again, builds the wheel, sdist, and `.mcpb`, writes `SHA256SUMS`, and publishes a GitHub Release with generated notes.

## Pull requests

- Keep each pull request focused, and explain what users will notice.
- Update `README.md` when behaviour, limits, or commands change for users, and this file when the workflow changes for developers.
- Never include real Garmin data, tokens, or account details in code, tests, issues, or screenshots.
