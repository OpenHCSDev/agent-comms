"""A killed restore must be completed by the next install, before it reads the bus."""

from agent_comms.active_route import resolve_comms_route

import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.active_route import active_route_path
from agent_comms.install_choices import RehearsalMode, UnnamedInstaller
from agent_comms.installer.backup import StoreBackup, UpgradeRefused
from agent_comms.installer.carry import Carried, Carry
from agent_comms.installer.launch import InstallReport
from agent_comms.installer.upgrade import upgrade
from agent_comms.registry_store import RegistryStore
from bus_record_fixture import prepare_native_comms


class RegistryTitleCarry(Carry):
    def carry(self, *, apply):
        registry = RegistryStore.at(self.root)
        if apply:
            with registry.editing() as edit:
                thread = edit.document.require("worker")
                edit.document.threads[thread.name] = replace(thread, title="Carried title")
        return Carried(1, rewritten=tuple(str(path.relative_to(self.root)) for path in registry.written))


KILLED_RESTORE = """
import os, signal, sys
from pathlib import Path
from agent_comms.installer.backup import StoreBackup
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.registry_store import RegistryStore

root, route, path = map(Path, sys.argv[1:4])
registry = RegistryStore.at(root)
publication = registry.written[int(sys.argv[4])]
original_replace = os.replace
def killed_publication(source, target, **directories):
    original_replace(source, target, **directories)
    if Path(target).name == publication.name:
        os.kill(os.getpid(), signal.SIGKILL)
os.replace = killed_publication
hold = MaintenanceBarrier(registry.path).begin(MaintenanceOperator.current())
with hold.holding():
    StoreBackup(root, route, path).restore()
raise AssertionError("The selected registry publication was never restored")
"""


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL and private registry require POSIX")
@pytest.mark.parametrize("publication", (0, 1), ids=("after-registry", "after-guard"))
def test_next_install_completes_a_killed_registry_restore(tmp_path, monkeypatch, publication):
    # HOME selects only this test's route. Neither process can replace the developer's route.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    comms = prepare_native_comms(Comms(resolve_comms_route(tmp_path / "wire")))
    comms.registry.declare(comms.registry.new_thread("worker", frozenset(), str(tmp_path)))
    registry = RegistryStore.at(comms.root)
    route = active_route_path()
    route.parent.mkdir(mode=0o700, parents=True)
    spill = tmp_path / "backups"
    # Put the guard first, as the interrupted historical backup did. Restore ordering
    # must come from the registry declaration, not from this manifest's ordering.
    backup = StoreBackup.take(comms.root, route, tuple(reversed(registry.written)), spill)
    expected = {path: StoreBackup.digest(path) for path in registry.written}
    RegistryTitleCarry(comms.root, comms.root, spill).carry(apply=True)
    killed = subprocess.run(
        [sys.executable, "-c", KILLED_RESTORE, str(comms.root), str(route), str(backup.path), str(publication)],
        capture_output=True, text=True, timeout=30,
    )
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    # Enter the real installer. Its next Git command deliberately fails: rollback
    # recovery must finish even before the checkout can be inspected or a stack built.
    with pytest.raises(UpgradeRefused, match="git failed"):
        upgrade(
            tmp_path / "missing-checkout", comms.root, "HEAD", UnnamedInstaller(), RehearsalMode(),
            False, spill, tmp_path / "install-scratch", lambda: None, InstallReport(),
        )
    assert {path: StoreBackup.digest(path) for path in registry.written} == expected
    assert comms.registry.require("worker").title != "Carried title"
