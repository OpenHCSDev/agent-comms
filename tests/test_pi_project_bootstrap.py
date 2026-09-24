"""The packaged Pi preload keeps managed resumed sessions in their selected project."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the Pi bootstrap")
def test_managed_pi_bootstrap_overrides_only_implicit_cli_session_cwd(tmp_path: Path) -> None:
    package = tmp_path / "pi-coding-agent"
    dist = package / "dist"
    core = dist / "core"
    core.mkdir(parents=True)
    (package / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    (core / "session-manager.js").write_text(
        "export class SessionManager { static open(path, dir, cwd) "
        '{ return cwd ?? "header-cwd"; } }',
        encoding="utf-8",
    )
    cli = dist / "cli.js"
    cli.write_text(
        'import {SessionManager} from "./core/session-manager.js"; '
        'console.log(JSON.stringify([SessionManager.open("saved"), '
        'SessionManager.open("saved", null, "explicit")]));',
        encoding="utf-8",
    )
    bootstrap = files("agent_comms").joinpath("pi_project_bootstrap.mjs")
    assert bootstrap.is_file()
    env = {
        **os.environ,
        "AGENT_COMMS_MANAGED": "1",
        "PI_WORKTREE": str(tmp_path),
        "NODE_OPTIONS": f"--import={bootstrap.as_uri()}",
        "PI_OFFLINE": "1",
    }
    node = shutil.which("node")
    assert node is not None
    result = subprocess.check_output([node, str(cli)], env=env, text=True)
    assert json.loads(result) == [str(tmp_path), "explicit"]

    (dist / "cli").mkdir()
    (dist / "cli" / "setup.js").write_text("export function setupCli() {}", encoding="utf-8")
    (dist / "main.js").write_text(
        'import {SessionManager} from "./core/session-manager.js"; '
        'export async function main() { console.log(JSON.stringify([SessionManager.open("saved"), '
        'SessionManager.open("saved", null, "explicit")])); }',
        encoding="utf-8",
    )
    (dist / "bundle").mkdir()
    bundled = dist / "bundle" / "cli.js"
    bundled.write_text('throw new Error("Bundled entry must not run twice");', encoding="utf-8")
    result = subprocess.check_output([node, str(bundled)], env=env, text=True)
    assert json.loads(result) == [str(tmp_path), "explicit"]

    env["AGENT_COMMS_MANAGED"] = "0"
    assert json.loads(subprocess.check_output([node, str(cli)], env=env, text=True)) == [
        "header-cwd",
        "explicit",
    ]
    env["AGENT_COMMS_MANAGED"] = "1"
    assert (
        subprocess.check_output(
            [node, "-"], input='console.log("stdin works")', env=env, text=True
        ).strip()
        == "stdin works"
    )
