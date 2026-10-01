"""Offline manifest validation, clean packaging, and real stdio startup."""

from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from jsonschema import Draft202012Validator
from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
BUILDER = runpy.run_path(str(ROOT / "scripts/build-chatgpt-plugin.py"))


@pytest.mark.parametrize("name", ["plugin", "mcp"])
def test_official_manifest_schema(name: str) -> None:
    schema = json.loads((ROOT / f"tests/schemas/{name}.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads((ROOT / f"{name}.json").read_text()))


def test_openai_metadata_and_shared_version() -> None:
    plugin = json.loads((ROOT / "plugin.json").read_text())
    claude = json.loads((ROOT / "manifest.json").read_text())
    assert plugin["name"] == claude["name"]
    assert plugin["version"] == claude["version"]
    interface = plugin["extensions"]["com.openai"]["interface"]
    assert interface["displayName"] == "Garmin Owl"
    assert len(interface["shortDescription"]) <= 30
    assert interface["capabilities"] == ["Read"]
    assert len(interface["defaultPrompt"]) <= 3


def test_package_files_cover_every_server_module() -> None:
    # A module missing from the allow-list only fails at import inside ChatGPT.
    package = ROOT / "src/garmin_owl"
    modules = {
        str(path.relative_to(ROOT))
        for path in package.iterdir()
        if path.suffix == ".py" or path.name == "py.typed"
    }
    assert modules <= set(BUILDER["PACKAGE_FILES"])


def test_launcher_is_executable_in_checkout() -> None:
    assert os.access(ROOT / "scripts/launch-chatgpt.sh", os.X_OK)


def stage(tmp_path: Path) -> Path:
    destination = tmp_path / "marketplace with spaces"
    BUILDER["build"](ROOT, destination)
    return destination / "plugins/garmin-owl"


def test_packaging_excludes_local_state_and_rebuilds_cleanly(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for relative in BUILDER["PACKAGE_FILES"]:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    for relative in (
        ".env",
        ".env.local",
        ".garminconnect/oauth1_token.json",
        "garmin_tokens.json",
        "src/garmin_owl/oauth2_token.json",
        "src/garmin_owl/secret.py",
        "cache.sqlite-wal",
        "src/garmin_owl/cache.db",
        ".venv/bin/python",
        "__pycache__/secret.pyc",
        "node_modules/local-state",
        "dist/old.mcpb",
    ):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic local state")
    destination = tmp_path / "marketplace"
    BUILDER["build"](source, destination)
    plugin = destination / "plugins/garmin-owl"
    (plugin / "stale.sqlite").touch()
    BUILDER["build"](source, destination)
    files = {str(path.relative_to(plugin)) for path in plugin.rglob("*") if path.is_file()}
    assert files == set(BUILDER["PACKAGE_FILES"])
    catalog = json.loads((destination / ".agents/plugins/marketplace.json").read_text())
    entry = catalog["plugins"][0]
    assert (destination / entry["source"]["path"]).resolve() == plugin.resolve()
    assert entry["policy"]["installation"] == "AVAILABLE"
    assert os.access(plugin / "scripts/launch-chatgpt.sh", os.X_OK)


def test_packaging_rejects_symlink_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.json").symlink_to(ROOT / "plugin.json")
    with pytest.raises(ValueError, match="without symlinks"):
        BUILDER["build"](source, tmp_path / "marketplace")


@pytest.mark.parametrize("location", ["bin", ".local/bin", ".cargo/bin"])
def test_launcher_resolves_uv_and_preserves_paths(tmp_path: Path, location: str) -> None:
    plugin = stage(tmp_path)
    home = tmp_path / "home with spaces"
    uv_dir = home / location
    uv_dir.mkdir(parents=True)
    uv = uv_dir / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 23\n')
    uv.chmod(0o755)
    result = subprocess.run(
        [str(plugin / "scripts/launch-chatgpt.sh"), str(plugin)],
        env={"HOME": str(home), "PATH": str(uv_dir) if location == "bin" else "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 23
    assert result.stdout.splitlines() == [
        "--directory",
        str(plugin),
        "run",
        "--locked",
        "garmin-owl",
    ]
    assert result.stderr == ""


def test_installed_plugin_starts_over_stdio(tmp_path: Path) -> None:
    # Exercise a relocated package with the manifest's actual command and env.
    # Reuse installed dependencies offline, but import the staged Python source.
    plugin = stage(tmp_path)
    config: dict[str, Any] = json.loads((plugin / "mcp.json").read_text())["mcpServers"][
        "garmin-owl"
    ]
    assert config["type"] == "stdio"
    assert config["command"].startswith("./")
    assert config["args"] == ["${PLUGIN_ROOT}"]
    assert config["cwd"] == "${PLUGIN_ROOT}"
    assert config["env"] == {"UV_PROJECT_ENVIRONMENT": "${PLUGIN_DATA}/venv"}
    env = dict(os.environ)
    env.update(
        {
            "PLUGIN_ROOT": str(plugin),
            "PLUGIN_DATA": str(tmp_path / "data"),
            "UV_PROJECT_ENVIRONMENT": sys.prefix,
            "UV_NO_SYNC": "1",
            "UV_OFFLINE": "1",
            "PYTHONPATH": str(plugin / "src"),
            "GARMINTOKENS": str(tmp_path / "absent-tokens"),
            "GARMIN_OWL_DB": str(tmp_path / "unused.sqlite"),
        }
    )

    async def check() -> None:
        with anyio.fail_after(30):
            async with Client(
                StdioServerParameters(
                    command=str(plugin / config["command"]),
                    args=[str(plugin)],
                    env=env,
                    cwd=plugin,
                )
            ) as client:
                result = await client.list_tools()
                expected = json.loads((ROOT / "manifest.json").read_text())["tools"]
                assert {tool.name for tool in result.tools} == {tool["name"] for tool in expected}
                response = await client.call_tool("get_activity", {"activity_id": "invalid"})
                assert response.is_error

    anyio.run(check)
    assert not (tmp_path / "absent-tokens").exists()
    assert not (tmp_path / "unused.sqlite").exists()
