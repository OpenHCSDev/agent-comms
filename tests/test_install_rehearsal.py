"""The install launch path, run the way an owner runs it, before any merge lands.

0765a002c broke every ``agent-comms install`` without ``--repos``, and install 21
refused because the target's owner probe asked the outgoing runtime for a method it no
longer had (fixed in #2419); every required check passed both, because nothing ran the
installer. This rehearses a real bus through the CLI under test, with the merge base's
build installed as the outgoing runtime: launch, target tree, staged stack build,
carried practice copy and the outgoing runtime's owner probe.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.backup import bus_files
from agent_comms.installer.practice import TestHome
from agent_comms.repo import Checkout, ScratchCheckout
from agent_comms.installer.upgrade import CarriedRehearsal, RehearsedUpgrade, Upgrade
from conftest import declare_test_bus, publish_initial

REPOSITORY = Path(__file__).resolve().parents[1]


def git(*arguments: str) -> str:
    return subprocess.run(["git", "-C", REPOSITORY, *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def installed_home(tmp_path_factory) -> dict[str, str]:
    """A home holding what an installed machine has: the installer's uv, and as the
    installed runtime a build of the merge base this change lands on. Both cases share
    it, so the second reuses the staged runtime the first built, as a second install
    would. Package caches stay shared."""
    home = tmp_path_factory.mktemp("installed-home")
    uv = home / ".local/state/agent-comms/upgrade-bus/uv"
    uv.parent.mkdir(parents=True)
    uv.symlink_to(TestHome(home).uv)
    caches = subprocess.run([uv, "cache", "dir"], check=True, capture_output=True, text=True)
    environment = {**os.environ, "HOME": str(home), "UV_CACHE_DIR": caches.stdout.strip(),
                   "npm_config_cache": str(Path.home() / ".npm")}
    environment.pop("PYTHONPATH", None)
    # The checkout owner adds the tree with its pinned submodules; `git archive` drops them.
    base = ScratchCheckout.add(REPOSITORY, home / "outgoing-source",
                               git("merge-base", "HEAD", "origin/main"), "--detach")
    runtime = home / ".local/share/agent-comms/runtimes/outgoing"
    try:
        subprocess.run([uv, "venv", "-q", "--python", sys.executable, runtime], check=True,
                       env=environment)
        subprocess.run([uv, "pip", "install", "-q", "--python", runtime / "bin/python", base.path],
                       check=True, env=environment)
    finally:
        base.retire()
    (home / ".local/share/agent-comms/current").symlink_to(runtime, target_is_directory=True)
    return environment


@pytest.mark.timeout(280)
@pytest.mark.parametrize("repos", ((), ("--repos",)), ids=("kept-repos", "given-repos"))
def test_install_rehearses_without_changing_the_bus(private_comms, installed_home, tmp_path, repos):
    comms = private_comms
    declare_test_bus(comms, channels=("comms",), threads=(
        comms.registry.new_thread(name="worker", tags=frozenset(), worktree=str(tmp_path)),))
    publish_initial(comms, "worker", "#all", "One message before the install")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", REPOSITORY, checkout], check=True)
    Checkout(checkout).prepare()
    before = bus_files(comms.root)
    given = (*repos, str(checkout)) if repos else ()
    installed = subprocess.run(
        [Path(sys.executable).with_name("agent-comms"), "--root", comms.root, "install",
         "--rehearse", "--checkout", checkout, "--to", "HEAD", "--project", checkout, *given],
        cwd=checkout, env={**installed_home, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        capture_output=True, text=True, timeout=270)
    assert installed.returncode == 0, installed.stderr
    result = FieldCodec.decode(Upgrade, FieldCodec.parse(installed.stdout))
    assert isinstance(result, RehearsedUpgrade) and isinstance(result.rehearsal, CarriedRehearsal)
    assert bus_files(comms.root) == before
