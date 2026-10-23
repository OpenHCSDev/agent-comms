"""The install launch path, run the way an owner runs it, before any merge lands.

0765a002c broke every ``agent-comms install`` without ``--repos``, and install 21
refused because the target's owner probe asked the outgoing runtime for a method it no
longer had (fixed in #2419); every required check passed both, because nothing ran the
installer. These checks rehearse an already-current bus format through the CLI:
launch, target tree, staged stack build, carried practice copy and the outgoing
runtime's owner probe. The outgoing package uses this source; the isolated target
records a distinct incoming revision while retaining every declared carry dependency.
The predecessor-history carry has its own real-writer fixture in test_carry_bus_history.
"""
from __future__ import annotations


import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.history_result import HistoryPageResult
from agent_comms.installer.backup import bus_files
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.practice import TestHome
from agent_comms.installer.upgrade import (
    CarriedRehearsal,
    CarriedRun,
    CreatedRun,
    InstalledUpgrade,
    RehearsedUpgrade,
    Stack,
    Upgrade,
)
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.maintenance_barrier import MaintenanceBarrier
from agent_comms.registry_store import RegistryStore
from agent_comms.repo import Checkout, ScratchCheckout
from agent_comms.root_runtime import RootRuntime
from agent_comms.storage_rules import StorageRule
from agent_comms.typed_table import ReconciledFile
from conftest import declare_test_bus, publish_initial

REPOSITORY = Path(__file__).resolve().parents[1]


def git(*arguments: str) -> str:
    return subprocess.run(["git", "-C", REPOSITORY, *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def installed_home(tmp_path_factory) -> dict[str, str]:
    """A home holding what an installed machine has: the installer's uv, and as the
    installed runtime a build of this source. Both cases share
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
    base = ScratchCheckout.add(REPOSITORY, home / "outgoing-source", git("rev-parse", "HEAD"), "--detach")
    try:
        outgoing = Stack.build(base.path, InstallCommands(), home)
    finally:
        base.retire()
    (home / ".local/share/agent-comms/current").symlink_to(
        outgoing.runtime.prefix, target_is_directory=True)
    return environment


@pytest.fixture
def installed_comms(private_comms, installed_home):
    """An installed machine's bus belongs to its installed runtime, not to the test
    process that declared it."""
    with pytest.MonkeyPatch.context() as installed:
        installed.setenv("HOME", installed_home["HOME"])
        RootRuntime.publish(private_comms.root, LaunchRuntime.installed())
    return private_comms


@pytest.fixture(scope="module")
def install_checkout(tmp_path_factory) -> Path:
    """Unchanged targets reuse one built revision; fault cases restore this checkout."""
    checkout = tmp_path_factory.mktemp("install-checkout") / "checkout"
    subprocess.run(["git", "clone", "--shared", "-q", REPOSITORY, checkout], check=True)
    Checkout(checkout).prepare()
    subprocess.run(["git", "-C", checkout, "-c", "user.name=Installer test",
                    "-c", "user.email=installer@example.invalid", "commit", "--allow-empty",
                    "-qm", "Record the incoming installer qualification revision"], check=True)
    return checkout


def drop_one_index(root: Path) -> str:
    """One declared store's schema drifts from both runtimes' declarations."""
    for path in sorted(ReconciledFile.existing_in(root)):
        with closing(sqlite3.connect(path)) as db:
            index = db.execute("SELECT name FROM sqlite_master WHERE type='index'"
                               " AND sql IS NOT NULL ORDER BY name LIMIT 1").fetchone()
            if index:
                db.execute(f'DROP INDEX "{index[0]}"')
                db.commit()
                return str(path.relative_to(root))
    raise AssertionError("No declared store has an index to drop")


@pytest.mark.timeout(280)
@pytest.mark.parametrize("repos, drifted", (
    ((), False), (("--repos",), True),
), ids=("kept-repos-current-stores", "given-repos-drifted-store"))
def test_install_rehearses_without_changing_the_bus(installed_comms, installed_home, install_checkout, tmp_path,
                                                    repos, drifted):
    """Retained carry steps inspect a practice copy, even when no step is pending.

    Only the deliberately drifted store is rewritten; neither case changes the bus.
    """
    comms = installed_comms
    declare_test_bus(comms, channels=("comms",), threads=(
        comms.registry.new_thread(name="worker", tags=frozenset(), worktree=str(tmp_path)),))
    publish_initial(comms, "worker", "#all", "One message before the install")
    rewritten = (drop_one_index(comms.root),) if drifted else ()
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
    assert isinstance(result, RehearsedUpgrade)
    assert isinstance(result.rehearsal, CarriedRehearsal)
    assert result.pending == ()
    assert result.rehearsal.files.names == rewritten
    assert bus_files(comms.root) == before


@pytest.mark.timeout(280)
def test_failed_live_migration_restores_data_checkout_and_runtime(
        installed_comms, installed_home, install_checkout, tmp_path):
    comms, checkout = installed_comms, install_checkout
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
    step.write_text('''from pathlib import Path
from agent_comms.installer.carry import Carry, Carried
from agent_comms.locked_store import DurableFile
class Saved(DurableFile):
    filename = "saved"
class Fault(Carry):
    opened_files = (Saved,)
    def carry(self, *, apply):
        if apply:
            (self.root / "saved").write_text("changed by the worker")
            if (self.root / "fail-live").exists():
                raise RuntimeError("intentional failure after writing the resource")
        return Carried(1, rewritten=(Path("saved"),))
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


@pytest.mark.timeout(280)
def test_failed_publication_restores_absent_route_without_store_changes(
        installed_comms, installed_home, install_checkout, tmp_path):
    checkout = install_checkout
    declare_test_bus(installed_comms, channels=("comms",))
    current = subprocess.check_output(
        ["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
    manifest = checkout / "pyproject.toml"
    manifest.write_text(manifest.read_text().replace(
        "[project.scripts]\n",
        '[project.scripts]\nroute-publication-fault = "agent_comms.cli:main"\n', 1))
    subprocess.run(["git", "-C", checkout, "add", manifest], check=True)
    subprocess.run(["git", "-C", checkout, "-c", "user.name=Installer test",
                    "-c", "user.email=installer@example.invalid", "commit", "-qm",
                    "Expose a publication failure after the route is rebound"], check=True)
    target = subprocess.check_output(
        ["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", checkout, "checkout", "--detach", "-q", current], check=True)

    home = tmp_path / "publication-home"
    uv = home / ".local/state/agent-comms/upgrade-bus/uv"
    uv.parent.mkdir(mode=0o700, parents=True)
    uv.symlink_to(Path(installed_home["HOME"]) / ".local/state/agent-comms/upgrade-bus/uv")
    outgoing = (Path(installed_home["HOME"]) / ".local/share/agent-comms/current").resolve()
    pointer = home / ".local/share/agent-comms/current"
    pointer.parent.mkdir(mode=0o700, parents=True)
    pointer.symlink_to(outgoing, target_is_directory=True)
    blocker = home / ".local/bin/route-publication-fault"
    blocker.parent.mkdir(mode=0o700, parents=True)
    blocker.write_text("An owner file that publication must preserve")
    route = home / ".local/state/agent-comms/active-route.json"
    assert not route.exists()

    completed = subprocess.run(
        [pointer / "bin/agent-comms", "--root", installed_comms.root, "install",
         "--checkout", checkout, "--to", target, "--project", checkout,
         "--spill-dir", tmp_path / "backups"], cwd=checkout,
        env={**installed_home, "HOME": str(home)}, capture_output=True, text=True, timeout=270,
    )
    assert completed.returncode != 0
    assert "Install rolled back:" in completed.stderr, completed.stderr
    assert "route-publication-fault" in completed.stderr
    assert not route.exists()
    assert pointer.resolve() == outgoing
    assert blocker.read_text() == "An owner file that publication must preserve"
    assert subprocess.check_output(
        ["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip() == current
    MaintenanceBarrier(RegistryStore.at(installed_comms.root).path).read().phase.new_sends.require_open()


@pytest.mark.timeout(280)
@pytest.mark.parametrize("fresh, live_kind", ((False, CarriedRun), (True, CreatedRun)),
                         ids=("retained-bus", "fresh-bus"))
def test_installed_entry_reports_completion_after_target_source_retirement(
        installed_comms, installed_home, install_checkout, tmp_path, fresh, live_kind):
    root = tmp_path / "new-bus" if fresh else installed_comms.root
    if not fresh:
        declare_test_bus(installed_comms, channels=("comms",))
    scratch = tmp_path / "install-scratch"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    StorageRule.path_in(root).write_text(json.dumps(FieldCodec.encode(
        StorageRule(scratch_base=scratch, floor_gib=10))))
    home = Path(installed_home["HOME"])
    outgoing = home / ".local/share/agent-comms/current/bin/agent-comms"
    # The caller and terminal publisher import installed packages. Only InstallRun's
    # existing target-source child borrows its temporary tree during the operation.
    completed = subprocess.run(
        [outgoing, "--root", root, "install", "--checkout", install_checkout,
         "--to", "HEAD", "--project", install_checkout,
         "--spill-dir", tmp_path / "backups"],
        cwd=install_checkout, env=installed_home, capture_output=True, text=True, timeout=270,
    )
    assert completed.returncode == 0, completed.stderr
    result = FieldCodec.decode(Upgrade, FieldCodec.parse(completed.stdout))
    assert isinstance(result, InstalledUpgrade) and type(result.live) is live_kind
    # The bus now belongs to the installed runtime, so its own CLI reads it.
    read = subprocess.run([outgoing, "--root", root, "history", "--target", "#comms"],
                          env=installed_home, capture_output=True, text=True, check=True, timeout=60)
    history = tuple(reversed(FieldCodec.decode(HistoryPageResult, FieldCodec.parse(read.stdout)).messages))
    assert len(history) == (1 if fresh else 2)
    assert history[-1].sender == "installer"
    assert history[-1].body.startswith("Install finished")
    if not fresh:
        assert history[0].body.startswith("Install starting")
    assert scratch.is_dir() and not tuple(scratch.iterdir())
