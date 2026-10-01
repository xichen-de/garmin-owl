#!/usr/bin/env python3
"""Stage an allow-listed local marketplace; never copy a working tree wholesale."""

from __future__ import annotations

import json
import shlex
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Explicit inputs prevent new token/cache files (even under src/) entering a bundle.
# tests/test_chatgpt_plugin.py fails if a module in src/garmin_owl is missing here.
PACKAGE_FILES = (
    "plugin.json",
    "mcp.json",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "scripts/launch-chatgpt.sh",
    "src/garmin_owl/__init__.py",
    "src/garmin_owl/py.typed",
    "src/garmin_owl/auth.py",
    "src/garmin_owl/cache_cli.py",
    "src/garmin_owl/client.py",
    "src/garmin_owl/database.py",
    "src/garmin_owl/diagnostic.py",
    "src/garmin_owl/models.py",
    "src/garmin_owl/normalize.py",
    "src/garmin_owl/notices.py",
    "src/garmin_owl/server.py",
    "src/garmin_owl/smoke.py",
    "src/garmin_owl/sync.py",
    "src/garmin_owl/tools.py",
)


def build(source: Path, destination: Path) -> None:
    """Replace a generated marketplace with a clean, relocatable source snapshot."""
    source = source.resolve()
    for relative in PACKAGE_FILES:
        path = source / relative
        if (
            not path.is_file()
            or path.is_symlink()
            or any((source / parent).is_symlink() for parent in Path(relative).parents)
        ):
            raise ValueError(f"Package input must be a regular file without symlinks: {relative}")
    if destination.is_symlink():
        raise ValueError("Marketplace destination must not be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging = Path(temporary) / "marketplace"
        plugin = staging / "plugins/garmin-owl"
        for relative in PACKAGE_FILES:
            target = plugin / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
        catalog = staging / ".agents/plugins/marketplace.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(
            json.dumps(
                {
                    "name": "garmin-owl-local",
                    "interface": {"displayName": "Garmin Owl Local"},
                    "plugins": [
                        {
                            "name": "garmin-owl",
                            "source": {"source": "local", "path": "./plugins/garmin-owl"},
                            "policy": {"installation": "AVAILABLE", "authentication": "ON_USE"},
                            "category": "Productivity",
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            shutil.rmtree(destination)
        staging.rename(destination)


def main() -> None:
    destination = ROOT / "dist/chatgpt-marketplace"
    build(ROOT, destination)
    print(f"Built local marketplace: {destination}")
    print(f"Register once with: codex plugin marketplace add {shlex.quote(str(destination))}")


if __name__ == "__main__":
    main()
