"""A killed restore must be completed by the next install, before it reads the bus."""


import os
import signal
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agent_comms.comms import Comms, wire
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.installer.active_route import active_route_path
from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.install_choices import RehearsalMode
from agent_comms.installer.launch import InstallReport
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.request import InstallRequest
from agent_comms.installer.upgrade import upgrade
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.registry.thread_text import GivenThreadText
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import InstallationFiles, LiveFilesRequest, RegistryFile
from bus_record_fixture import prepare_native_comms


class RegistryTitleCarry(Carry):
    opened_files = (RegistryFile,)
    def carry(self, *, apply):
        registry = RegistryStore.at(self.root)
        if apply:
            with registry.editing() as edit:
                thread = edit.document.require("worker")
                edit.document.threads[thread.name] = replace(thread, title=GivenThreadText("Carried title"))
                edit.commit()
        return Carried(1, rewritten=tuple(path.relative_to(self.root) for path in registry.written))


KILLED_RESTORE = """
import os, signal, sys
from pathlib import Path
from agent_comms.installer.backup import StoreBackup
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.registry.registry_store import RegistryStore

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
    StoreBackup(root, route, path).restore(lambda: None)
raise AssertionError("The selected registry publication was never restored")
"""


@dataclass(frozen=True)
class CarryingRoot:
    """A private bus with one declared thread, its route and its backup spill: the root an
    install finds after a carry that a backup preceded."""

    tmp_path: Path
    comms: Comms

    @classmethod
    def prepared(cls, tmp_path: Path) -> "CarryingRoot":
        comms = prepare_native_comms(wire(tmp_path / "wire"))
        comms.registry.declare(NativeThread("worker", frozenset(), Checkout(tmp_path)))
        active_route_path().parent.mkdir(mode=0o700, parents=True)
        return cls(tmp_path, comms)

    @property
    def registry(self) -> RegistryStore:
        return RegistryStore.at(self.comms.root)

    @property
    def spill(self) -> Path:
        return self.tmp_path / "backups"

    def backup(self, written: tuple[Path, ...]) -> StoreBackup:
        return StoreBackup.take(self.comms.root, active_route_path(), written, self.spill)

    def carry(self) -> None:
        RegistryTitleCarry(LiveFilesRequest(self.comms.root), self.spill).carry(apply=True)

    def digests(self) -> dict[Path, str]:
        return {
            resource.live(self.comms.root): resource.owner.digest(resource.live(self.comms.root))
            for resource in InstallationFiles(LiveFilesRequest(self.comms.root)).owned_files(
                self.registry.written)
        }

    def refused_install(self) -> None:
        """Enter the real installer; its next Git command fails on the missing checkout, so
        rollback recovery must finish before a checkout can be inspected or a stack built."""
        with pytest.raises(UpgradeRefused, match="missing-checkout"):
            upgrade(
                InstallRequest(checkout=self.tmp_path / "missing-checkout",
                               mode=RehearsalMode(), spill_dir=self.spill),
                self.comms.root, "HEAD", self.tmp_path / "install-scratch",
                LaunchRuntime.running(), lambda: None, InstallReport(),
            )


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL and private registry require POSIX")
@pytest.mark.parametrize("publication", (0, 1), ids=("after-registry", "after-guard"))
def test_next_install_completes_a_killed_registry_restore(tmp_path, installed_runtime, publication):
    # The temporary installation's HOME selects only this test's route and runtime.
    root = CarryingRoot.prepared(tmp_path)
    # Put the guard first, as the interrupted historical backup did. Restore ordering
    # must come from the registry declaration, not from this manifest's ordering.
    backup = root.backup(tuple(reversed(root.registry.written)))
    expected = root.digests()
    root.carry()
    killed = subprocess.run(
        [sys.executable, "-c", KILLED_RESTORE, str(root.comms.root), str(active_route_path()),
         str(backup.path), str(publication)],
        capture_output=True, text=True, timeout=30,
    )
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    root.refused_install()
    assert root.digests() == expected
    assert root.comms.registry.require("worker").title != GivenThreadText("Carried title")


def test_next_install_leaves_a_completed_installs_backup_alone(tmp_path, installed_runtime):
    # Install 24: every completed install leaves a backup that differs from the live root
    # it carried. Differing is not a restore that began, so the next install must neither
    # roll the bus back to it nor take the maintenance hold.
    root = CarryingRoot.prepared(tmp_path)
    backup = root.backup(root.registry.written)
    root.carry()
    carried = root.digests()
    assert not backup.original()
    root.refused_install()
    assert root.digests() == carried
    assert root.comms.registry.require("worker").title == GivenThreadText("Carried title")
    assert backup.path.exists()
