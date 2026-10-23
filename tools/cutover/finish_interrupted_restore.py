"""Finish the restore install 24 began and did not complete on the live root, once.

Install 24's ``complete_interrupted`` (before #2779) took install 22's backup for an
interrupted restore and copied it over the live root. It replaced ``.root-runtime.json``,
``.registry-owner-guard``, ``bus.sqlite3`` and ``coordination.sqlite3`` with their
pre-install-22 contents, then died on a selected-tool ledger directory install 22's carry
had deleted. The root is half pre-22 and half post-22, owned by runtime f3f069fb8 while
``current`` selects 701746802, so no client attaches.

This finishes that restore with the installer's own ``StoreBackup.restore`` under the
maintenance hold, then selects the runtime the restored root names through the installer's
own ``Stack.publish``: the rollback path's "original runtime selected again". The backup
directory is the only complete copy and is never retired or renamed here. Run it once on
the live root, then delete it.

Run it with an installed runtime's interpreter: the backup's digests are SQLite dumps
taken with the runtimes' SQLite 3.50.4, and a dev environment's newer SQLite dumps the
same bus differently, so the restore would refuse its own result.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from agent_comms.active_route import active_route_path, route_directory
from agent_comms.host_services import HostService
from agent_comms.installer.backup import StoreBackup, UpgradeRefused
from agent_comms.installer.upgrade import InstallCommands, Stack
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.private_path import FileRevision
from agent_comms.registry_store import RegistryStore
from agent_comms.root_runtime import RootRuntimeStore


@dataclass(frozen=True)
class InterruptedRestore:
    root: Path
    backup: Path
    runtime: Path
    checkout: Path
    home: Path

    @property
    def store_backup(self) -> StoreBackup:
        return StoreBackup(self.root, active_route_path(), self.backup)

    @property
    def stack(self) -> Stack:
        return Stack(self.checkout, InstallCommands(), self.home,
                     LaunchRuntime(self.runtime / "bin/python"))

    def require_half_restored(self) -> None:
        """Only the root this backup's restore already began on: its runtime record is
        the backup's byte for byte."""
        name = RootRuntimeStore.path_in(self.root).relative_to(self.root)
        if (self.root / name).read_bytes() != (self.backup / "stores" / name).read_bytes():
            raise UpgradeRefused(f"{self.root} does not hold {self.backup}'s runtime record")
        if not self.stack.python.exists():
            raise UpgradeRefused(f"No runtime at {self.stack.python}")

    def finish(self) -> None:
        self.require_half_restored()
        backup = self.store_backup
        with route_directory(backup.route):
            backup.route_revision = FileRevision.capture(backup.route)
        maintenance = MaintenanceBarrier(RegistryStore.at(self.root).path)
        hold = maintenance.begin(MaintenanceOperator.current())
        with hold.holding():
            backup.restore()
            self.stack.publish(self.root)
        maintenance.clear(hold)

    def run(self) -> None:
        """Nothing may write the root while it is restored; services start again on the
        runtime that owns it."""
        stopped = HostService.stop_running()
        self.finish()
        self.stack.start_services(stopped)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    arguments = parser.parse_args()
    InterruptedRestore(arguments.root, arguments.backup, arguments.runtime,
                       arguments.checkout, Path.home()).run()
    print(f"Restored {arguments.root} from {arguments.backup}; runtime {arguments.runtime}")


if __name__ == "__main__":
    main()
