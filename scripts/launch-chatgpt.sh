#!/bin/sh
# Resolve uv without relying on a GUI app inheriting an interactive shell PATH.
set -eu

if [ "$#" -ne 1 ] || [ ! -f "$1/pyproject.toml" ]; then
    printf '%s\n' 'Garmin Owl: expected the installed plugin root as the only argument.' >&2
    exit 1
fi

uv_bin=$(command -v uv 2>/dev/null || true)
if [ -z "$uv_bin" ]; then
    for candidate in \
        "${HOME:-}/.local/bin/uv" \
        "${HOME:-}/.cargo/bin/uv" \
        /opt/homebrew/bin/uv \
        /home/linuxbrew/.linuxbrew/bin/uv \
        /usr/local/bin/uv \
        /usr/bin/uv \
        /snap/bin/uv; do
        if [ -f "$candidate" ] && [ -x "$candidate" ]; then
            uv_bin=$candidate
            break
        fi
    done
fi

if [ -z "$uv_bin" ]; then
    printf '%s\n' \
        'Garmin Owl: uv was not found. Install uv from https://docs.astral.sh/uv/getting-started/installation/ (or brew install uv on macOS), then restart ChatGPT Desktop.' \
        'Checked PATH, ~/.local/bin, ~/.cargo/bin, /opt/homebrew/bin, /home/linuxbrew/.linuxbrew/bin, /usr/local/bin, /usr/bin, and /snap/bin. For a custom install, link uv into ~/.local/bin.' >&2
    exit 127
fi

# The host sets UV_PROJECT_ENVIRONMENT to PLUGIN_DATA/venv. Tokens and the
# Garmin database retain their existing locations; stdout belongs to MCP.
exec "$uv_bin" --directory "$1" run --locked garmin-owl
