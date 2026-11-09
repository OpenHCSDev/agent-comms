"""One bounded installer read-back diagnostic; no pytest or migration is run."""
from __future__ import annotations

from pathlib import Path

from agent_comms.comms import wire
from agent_comms.execution.launch_runtime import LaunchRuntime, RestoredRuntime
from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.interpreter_operation import TargetProgram
from agent_comms.installer.upgrade import PreparedMigration, Stack, TargetStack
from agent_comms.messaging.bus import Bus
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.storage.locked_store import LiveFilesRequest
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator

OUT = Path(__file__).resolve().parent
CASE = OUT / 'case'
SOURCE = Path('/home/ts/wt/comms-friction--P17')


class DiagnosticProgram(TargetProgram):
    def entry(self):
        return (OUT / 'worker.py',)


def main():
    CASE.mkdir()
    root = CASE / 'comms'
    Bus.install(Bus.path_in(root))
    StorageRule(floor_gib=0).prepare_isolated(root)
    comms = wire(root)
    saved = root / 'saved'
    saved.write_text('original')
    (root / 'fail').touch()
    steps = CASE / 'steps'
    steps.mkdir()
    with InstallCommands() as commands:
        stack = Stack(SOURCE, commands, home=CASE,
                      runtime=RestoredRuntime(LaunchRuntime.running().interpreter))
        staged = stack.extended(TargetStack, program=DiagnosticProgram(steps))
        maintenance = MaintenanceBarrier(RegistryStore.at(root).path)
        hold = maintenance.begin(MaintenanceOperator.current())
        try:
            with hold.holding():
                print('backup', flush=True)
                StoreBackup.take(root, root / 'no-route', (saved,), CASE / 'spill')
                print('capture / worker read-back', flush=True)
                migration = PreparedMigration.capture(hold, staged, LiveFilesRequest(root))
                print('captured', migration.read, flush=True)
                migration.pins.close()
        finally:
            maintenance.clear(hold)


if __name__ == '__main__':
    main()
