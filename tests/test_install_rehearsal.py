"""The install launch path, run the way an owner runs it, before any merge lands.

0765a002c broke every ``agent-comms install`` without ``--repos`` and passed every
required check, because nothing ran the installer. This rehearses a real bus through
the installed CLI: launch, target tree, staged stack build and carried practice copy.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.upgrade import CarriedRehearsal, RehearsedUpgrade, Upgrade
from conftest import declare_test_bus, publish_initial

REPOSITORY = Path(__file__).resolve().parents[1]


def saved_bytes(root: Path) -> dict[str, tuple[str, int]]:
    return {str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(),
                                          path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.fixture(scope="module")
def installed_home(tmp_path_factory) -> dict[str, str]:
    """A home holding only what an installed machine has: the installer's uv and the
    runtime this suite runs from as the installed one. Both cases share it, so the
    second reuses the staged runtime the first built, as a second install would."""
    home = tmp_path_factory.mktemp("installed-home")
    uv = home / ".local/state/agent-comms/upgrade-bus/uv"
    uv.parent.mkdir(parents=True)
    uv.symlink_to(shutil.which("uv"))
    current = home / ".local/share/agent-comms/current"
    current.parent.mkdir(parents=True)
    current.symlink_to(Path(sys.prefix), target_is_directory=True)
    caches = subprocess.run([uv, "cache", "dir"], check=True, capture_output=True, text=True)
    return {**os.environ, "HOME": str(home), "UV_CACHE_DIR": caches.stdout.strip(),
            "npm_config_cache": str(Path.home() / ".npm")}


@pytest.mark.timeout(280)
@pytest.mark.parametrize("repos", ((), ("--repos",)), ids=("kept-repos", "given-repos"))
def test_install_rehearses_without_changing_the_bus(private_comms, installed_home, tmp_path, repos):
    comms = private_comms
    declare_test_bus(comms, threads=(
        comms.registry.new_thread(name="worker", tags=frozenset(), worktree=str(tmp_path)),))
    publish_initial(comms, "worker", "#all", "One message before the install")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", REPOSITORY, checkout], check=True)
    before = saved_bytes(comms.root)
    given = (*repos, str(checkout)) if repos else ()
    installed = subprocess.run(
        [Path(sys.executable).with_name("agent-comms"), "--root", comms.root, "install",
         "--rehearse", "--checkout", checkout, "--to", "HEAD", "--project", checkout, *given],
        cwd=checkout, env=installed_home, capture_output=True, text=True, timeout=270)
    assert installed.returncode == 0, installed.stderr
    result = FieldCodec.decode(Upgrade, FieldCodec.parse(installed.stdout))
    assert isinstance(result, RehearsedUpgrade) and isinstance(result.rehearsal, CarriedRehearsal)
    assert saved_bytes(comms.root) == before
