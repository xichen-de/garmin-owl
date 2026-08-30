#!/usr/bin/env python3
"""Verify a release tag matches every package/extension version declaration."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if len(sys.argv) != 2 or not re.fullmatch(r"v\d+\.\d+\.\d+", sys.argv[1]):
        raise SystemExit("usage: check-release-version.py vMAJOR.MINOR.PATCH")
    expected = sys.argv[1][1:]
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project_version = str(tomllib.load(handle)["project"]["version"])
    with (ROOT / "manifest.json").open(encoding="utf-8") as handle:
        manifest_version = str(json.load(handle)["version"])
    init_text = (ROOT / "src/garmin_owl/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', init_text, re.MULTILINE)
    init_version = match.group(1) if match else "missing"
    server_text = (ROOT / "src/garmin_owl/server.py").read_text(encoding="utf-8")
    server_match = re.search(r'^    version="([^"]+)",$', server_text, re.MULTILINE)
    server_version = server_match.group(1) if server_match else "missing"
    versions = {
        "pyproject.toml": project_version,
        "manifest.json": manifest_version,
        "src/garmin_owl/__init__.py": init_version,
        "src/garmin_owl/server.py": server_version,
    }
    mismatches = {name: version for name, version in versions.items() if version != expected}
    if mismatches:
        details = ", ".join(f"{name}={version}" for name, version in mismatches.items())
        raise SystemExit(f"tag version {expected} does not match: {details}")
    print(f"Release versions match {sys.argv[1]}")


if __name__ == "__main__":
    main()
