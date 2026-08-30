#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"
version=$(python3 -c 'import json; print(json.load(open("manifest.json"))["version"])')
output="$repo_dir/dist/garmin-owl-$version.mcpb"

npx --yes @anthropic-ai/mcpb@2.1.2 validate manifest.json
npx --yes @anthropic-ai/mcpb@2.1.2 pack . "$output"
printf '%s\n' "Built $output"
