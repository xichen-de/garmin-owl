#!/bin/sh
set -eu

for tool in uv node npx; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf '%s\n' "Missing $tool. Install uv and Node.js LTS with npm, then retry. See README.md: Build the Desktop extension." >&2
        exit 1
    fi
done

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"
version=$(uv run --locked python -c 'import json; print(json.load(open("manifest.json"))["version"])')
uv run --locked python scripts/check-release-version.py "v$version"
output="$repo_dir/dist/garmin-owl-$version.mcpb"

npx --yes @anthropic-ai/mcpb@2.1.2 validate manifest.json
mkdir -p "$repo_dir/dist"
npx --yes @anthropic-ai/mcpb@2.1.2 pack . "$output"
printf '%s\n' "Built $output"
