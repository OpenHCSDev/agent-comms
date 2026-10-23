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
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.backup import bus_files
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.practice import TestHome
from agent_comms.installer.upgrade import (
    CarriedRehearsal,
    RehearsedUpgrade,
    Stack,
    UncopiedRehearsal,
    Upgrade,
)
from agent_comms.maintenance_barrier import MaintenanceBarrier
from agent_comms.registry_store import RegistryStore
from agent_comms.repo import Checkout, ScratchCheckout
from agent_comms.typed_table import ReconciledFile
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
    try:
        outgoing = Stack.build(base.path, InstallCommands(), home)
    finally:
        base.retire()
    (home / ".local/share/agent-comms/current").symlink_to(
        outgoing.runtime.prefix, target_is_directory=True)
    return environment


@pytest.fixture
def install_checkout(tmp_path) -> Path:
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "--shared", "-q", REPOSITORY, checkout], check=True)
    Checkout(checkout).prepare()
    return checkout


def drop_one_index(root: Path) -> None:
    """One declared store's schema drifts from both runtimes' declarations."""
    for path in sorted(ReconciledFile.existing_in(root)):
        with closing(sqlite3.connect(path)) as db:
            index = db.execute("SELECT name FROM sqlite_master WHERE type='index'"
                               " AND sql IS NOT NULL ORDER BY name LIMIT 1").fetchone()
            if index:
                db.execute(f'DROP INDEX "{index[0]}"')
                db.commit()
                return
    raise AssertionError("No declared store has an index to drop")


@pytest.mark.timeout(280)
@pytest.mark.parametrize("repos, rehearsal", (
    ((), UncopiedRehearsal), (("--repos",), CarriedRehearsal),
), ids=("kept-repos-nothing-to-carry", "given-repos-drifted-store"))
def test_install_rehearses_without_changing_the_bus(private_comms, installed_home, install_checkout, tmp_path,
                                                    repos, rehearsal):
    """A bus with nothing to carry is rehearsed without a copy; one store whose schema
    drifted makes the rehearsal copy the bus and carry it. Neither changes the bus."""
    comms = private_comms
    declare_test_bus(comms, channels=("comms",), threads=(
        comms.registry.new_thread(name="worker", tags=frozenset(), worktree=str(tmp_path)),))
    publish_initial(comms, "worker", "#all", "One message before the install")
    if rehearsal is CarriedRehearsal:
        drop_one_index(comms.root)
    checkout = install_checkout
    before = bus_files(comms.root)
    given = (*repos, str(checkout)) if repos else ()
    installed = subprocess.run(
        [Path(sys.executable).with_name("agent-comms"), "--root", comms.root, "install",
         "--rehearse", "--checkout", checkout, "--to", "HEAD", "--project", checkout, *given],
        cwd=checkout, env={**installed_home, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        capture_output=True, text=True, timeout=270)
    assert installed.returncode == 0, installed.stderr
    result = FieldCodec.decode(Upgrade, FieldCodec.parse(installed.stdout))
    assert isinstance(result, RehearsedUpgrade) and type(result.rehearsal) is rehearsal
    assert bus_files(comms.root) == before


@pytest.mark.timeout(280)
def test_failed_live_migration_restores_data_checkout_and_runtime(
        private_comms, installed_home, install_checkout, tmp_path):
    comms, checkout = private_comms, install_checkout
    declare_test_bus(comms, channels=("comms",), threads=(
        comms.registry.new_thread(name="worker", tags=frozenset(), worktree=str(tmp_path)),))
    publish_initial(comms, "worker", "#all", "Retained before migration")
    saved = comms.root / "saved"
    saved.write_text("original")
    # This trigger is not a declared store or backup resource, so practice copying
    # leaves it behind. The same real worker succeeds in rehearsal and fails live.
    (comms.root / "fail-live").touch()
    current = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
    step = checkout / "tools/cutover/carry_fault.py"
    step.write_text('''from agent_comms.installer.carry import Carry, Carried
class Fault(Carry):
    def backup_files(self):
        return (self.root / "saved",)
    def carry(self, *, apply):
        if apply:
            (self.root / "saved").write_text("changed by the worker")
            if (self.root / "fail-live").exists():
                raise RuntimeError("intentional failure after writing the resource")
        return Carried(1, rewritten=("saved",))
''')
    subprocess.run(["git", "-C", checkout, "add", step], check=True)
    subprocess.run(["git", "-C", checkout, "-c", "user.name=Installer test",
                    "-c", "user.email=installer@example.invalid", "commit", "-qm",
                    "Migration that fails after writing on the live bus"], check=True)
    target = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", checkout, "checkout", "--detach", "-q", current], check=True)
    home = Path(installed_home["HOME"])
    pointer = home / ".local/share/agent-comms/current"
    outgoing = pointer.resolve()
    installed = subprocess.run(
        [Path(sys.executable).with_name("agent-comms"), "--root", comms.root, "install",
         "--checkout", checkout, "--to", target, "--project", checkout,
         "--spill-dir", tmp_path / "backups"],
        cwd=checkout, env={**installed_home, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        capture_output=True, text=True, timeout=270)
    assert installed.returncode != 0
    assert "Install rolled back:" in installed.stderr, installed.stderr
    assert "intentional failure after writing the resource" in installed.stderr
    assert saved.read_text() == "original"
    assert pointer.resolve() == outgoing
    assert (home / ".local/bin/agent-comms").resolve() == outgoing / "bin/agent-comms"
    assert subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip() == current
    MaintenanceBarrier(RegistryStore.at(comms.root).path).read().phase.new_sends.require_open()
    assert comms.registry.require("worker").name == "worker"
