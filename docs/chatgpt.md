# Using garmin-owl in ChatGPT Desktop

`garmin-owl` can also run as a local plugin in ChatGPT Desktop. It is the same Python server, packaged for ChatGPT's local plugin marketplace. It does not work in ChatGPT on the web or on mobile, and a URL-based developer-mode connector can't run it.

## ChatGPT requirements

- A current ChatGPT Desktop build on macOS or Ubuntu with **Work mode or Codex**, local plugin marketplaces, and stdio MCP plugins. Availability can vary by app version, account, and workspace policy.
- The `codex` CLI with marketplace support. Check with `codex plugin marketplace add --help`.
- Git and `uv`, as in [What you need](../README.md#what-you-need).

See OpenAI's [local plugin documentation](https://developers.openai.com/plugins/build/plugins) and the [Agent Plugins 1.0.0 specification](https://agent-plugins.org/specification) for background.

## Install

1. In your `garmin-owl` folder, as the same OS user that runs ChatGPT, sign in (skip if you did [step 1 of the README](../README.md#1-sign-in-to-garmin) already) and build the local marketplace:

   ```bash
   uv sync --locked
   uv run garmin-owl-auth
   uv run --locked python scripts/build-chatgpt-plugin.py
   ```

2. Register the marketplace (once):

   ```bash
   codex plugin marketplace add ./dist/chatgpt-marketplace
   ```

3. Restart ChatGPT Desktop, open the **Plugins Directory**, choose **Garmin Owl Local**, then install and enable **Garmin Owl**.
4. Start a new local Work/Codex chat and ask, for example, "Summarize my recovery today."

The first launch needs internet access: it installs the locked Python dependencies into the plugin's private data folder (`PLUGIN_DATA/venv`). After that it runs offline apart from reading Garmin.

<details>
<summary><b>What does the build script put in the plugin?</b></summary>

`scripts/build-chatgpt-plugin.py` writes `dist/chatgpt-marketplace/`, containing the plugin under `plugins/garmin-owl` and a catalog at `.agents/plugins/marketplace.json` that uses only relative paths. It copies an explicit list of program files and never your `.venv`, tokens, credentials, SQLite files, or other checkout state. Register this generated folder, not the checkout itself.

</details>

## Updating the ChatGPT plugin

ChatGPT runs its own installed copy of the plugin, so after `git pull`:

1. Rebuild with `uv run --locked python scripts/build-chatgpt-plugin.py`.
2. Restart ChatGPT Desktop.
3. Uninstall **Garmin Owl** and install it again from **Garmin Owl Local**.

`codex plugin marketplace upgrade` only refreshes Git marketplaces, so it doesn't apply here.

## Sign-in, data, and settings

- Sign in only with `uv run garmin-owl-auth` in your terminal. Never type your Garmin password into ChatGPT.
- The plugin reuses `~/.garminconnect` and the same [local cache](reference.md#local-cache) as the terminal commands. Both stay outside the plugin package.
- Tool results are sent to ChatGPT as part of your conversation, as with any MCP client.
- `GARMINTOKENS` and `GARMIN_OWL_DB` exported in your shell don't reach GUI apps such as ChatGPT. The plugin uses the default locations unless those variables are set in ChatGPT's own environment.

## ChatGPT troubleshooting

- **"uv was not found":** desktop apps often don't inherit your shell's `PATH`. The launcher looks for `uv` on `PATH` and in `~/.local/bin`, `~/.cargo/bin`, `/opt/homebrew/bin`, `/home/linuxbrew/.linuxbrew/bin`, `/usr/local/bin`, `/usr/bin`, and `/snap/bin`. Install `uv` with its [official installer](https://docs.astral.sh/uv/getting-started/installation/) (or `brew install uv` on macOS), or link a custom install into `~/.local/bin` (`ln -s "$(command -v uv)" ~/.local/bin/uv`), then restart ChatGPT.
- **Garmin Owl Local isn't listed:** check `codex plugin marketplace list`, restart ChatGPT, and confirm your build and account support local marketplaces and stdio plugins.
- **Old behaviour after updating:** reinstall the plugin as described in [Updating the ChatGPT plugin](#updating-the-chatgpt-plugin).
