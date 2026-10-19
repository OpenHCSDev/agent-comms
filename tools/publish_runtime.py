"""Core's idle publication transaction, called by the qualified runtime builder.

Core owns maintenance, exact worker selection, stopped cutover and recovery.
The frontend supplies a context owning its readers; it enters only after drain.
"""
from __future__ import annotations

import argparse
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from abc import abstractmethod
import hashlib
from importlib.metadata import distribution
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from agent_comms.active_route import read_active_route, active_route_path, acquire_exclusive_route_lock
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec, SubclassUnion, NullMember, RecordMember
from agent_comms.harness_install import Harness, replace_link
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.maintenance_barrier import MaintenanceOperator, MaintenanceState
from agent_comms.owner_cutover import StoppedOwnerInstallation, MaintainedOwnerCutover
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.owner_restart import OwnerRestartRequest, StoppedOwnerBatch
from agent_comms.restart_refusals import OwnerBusyRefusal
from agent_comms.store_files import _atomic_write_text, _store_lock
from .cutover.carry import CarryLedger, PreparedCarries
from .cutover.backup import StoreBackup


class PublicationIntent(SubclassUnion, affix=''):
    """A new publication selects workers; an interrupted one keeps its exact selection."""

    @classmethod
    def read(cls, path: Path) -> PublicationIntent:
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            return NoPublicationIntent()
        return FieldCodec.decode(SavedPublicationIntent, FieldCodec.parse(payload))

    @abstractmethod
    def require_target(self, runtime: Path, previous: Path) -> None: ...

    @abstractmethod
    def select(self, snapshot, selected, runtime: Path, previous: Path,
               path: Path) -> SavedPublicationIntent: ...


class NoPublicationIntent(NullMember, PublicationIntent):
    def require_target(self, runtime: Path, previous: Path) -> None:
        if previous == runtime:
            raise ValueError('Runtime is already current')

    def select(self, snapshot, selected, runtime: Path, previous: Path,
               path: Path) -> SavedPublicationIntent:
        intent = SavedPublicationIntent(str(runtime), str(previous), tuple(
            OwnerRestartSelection.capture(snapshot, thread.name) for thread in selected))
        _atomic_write_text(path, json.dumps(FieldCodec.encode(intent)) + '\n', fsync_parent=True)
        return intent


@dataclass(frozen=True)
class SavedPublicationIntent(RecordMember, PublicationIntent):
    """Installation witnesses, not current process/turn state or permission to replay."""
    runtime: str
    previous: str
    owners: tuple[OwnerRestartSelection, ...]

    def require_target(self, runtime: Path, previous: Path) -> None:
        if self.runtime != str(runtime) or previous not in (Path(self.previous), runtime):
            raise ValueError('An unfinished publication belongs to another runtime')

    def select(self, snapshot, selected, runtime: Path, previous: Path,
               path: Path) -> SavedPublicationIntent:
        return self


@dataclass(frozen=True)
class RuntimePublication:
    runtime: Path
    links: dict[Path, Path]

    @classmethod
    def installed(cls) -> RuntimePublication:
        binary = Path(sys.executable).parent.resolve(strict=True)
        links = {
            Path.home() / '.local/bin' / entry.name: binary / entry.name
            for package in ('agent-comms', 'batrachian-toad')
            for entry in distribution(package).entry_points
            if entry.group == 'console_scripts'
        }
        links |= Harness.published_links(Path.home())
        for link, target in links.items():
            target.stat()
            if link.exists() and not link.is_symlink():
                raise FileExistsError(f'Publication cannot replace a regular file: {link}')
        return cls(binary.parent, links)

    def require_cli(self, root: Path, command: str = '--help') -> None:
        result = subprocess.run(
            [self.runtime / 'bin/agent-comms', '--root', str(root), command],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode or result.stderr:
            raise RuntimeError(
                f'Runtime CLI refused publication (exit {result.returncode}): {result.stdout}{result.stderr}'
            )

    def publish(self, root: Path, *, checkout: Path, timeout: float = 1200,
                resources: AbstractContextManager = nullcontext()) -> tuple[str, ...]:
        """Wait, stop, switch, resume, and release maintenance on every normal exit.

        Re-entry reads our existing hold and exact interrupted selection. No
        input is resubmitted. Unrelated maintenance and changed incarnations
        are refused rather than guessed.
        """
        backup = self.unfinished_backup(root)
        ledger = CarryLedger(backup.data_root if backup is not None else root)
        carries = PreparedCarries.at(checkout, root, ledger.applied())
        self.require_cli(root)
        path = active_route_path()
        route = read_active_route(path)
        if route is None or route.root != root.resolve():
            raise ValueError('Publication requires the selected active root')
        comms = wire(route)
        barrier = comms.owners.maintenance
        operator = MaintenanceOperator('runtime-publication:' + hashlib.sha256(
            os.fsencode(self.runtime.resolve())).hexdigest())
        intent_path = root / 'runtime-publication.json'
        with _store_lock(root / 'runtime-publication'):
            previous = StoppedOwnerInstallation.current_pointer().resolve(strict=True)
            intent = PublicationIntent.read(intent_path)
            intent.require_target(self.runtime, previous)
            current = barrier.read()
            hold = current.phase.hold_for(barrier, current, operator)
            try:
                deadline = time.monotonic() + timeout
                previous_busy = ()
                while True:
                    snapshot = comms.registry.snapshot()
                    selected = OwnerRestartRequest().threads(snapshot)
                    busy = []
                    for thread in selected:
                        try:
                            comms.owners.require_restart_idle(thread)
                        except OwnerBusyRefusal:
                            busy.append(thread.name)
                    if not busy:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Active turns still running: ' + ', '.join(busy))
                    if tuple(busy) != previous_busy:
                        print('Waiting for active turns: ' + ', '.join(busy), flush=True)
                        previous_busy = tuple(busy)
                    time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                intent = intent.select(snapshot, selected, self.runtime, previous, intent_path)
                snapshot = comms.registry.snapshot()
                remaining = []
                for original in intent.owners:
                    thread = snapshot.threads[original.name]
                    if thread.incarnation != original.identity.incarnation:
                        raise ValueError('Selected owner incarnation changed: ' + original.name)
                    if thread.process_identity == original.process and thread.process_alive:
                        remaining.append(original.name)
                request = OwnerRestartRequest(names=tuple(remaining),
                    source_interpreter=intent.previous + '/bin/python')
                backup = self.unfinished_backup(root)
                if backup is not None and not backup.original():
                    if any(original.process.alive() for original in intent.owners):
                        raise RuntimeError('Cannot restore data while an original worker is running')
                    with _store_lock(root / 'wire'), self.selected_route(root):
                        self.restore_backup(backup)
                with ExitStack() as publication:
                    publication.enter_context(resources)
                    # Runs before reader restoration, including when stopping or
                    # conversion failed. Unconfirmed data keeps readers stopped.
                    publication.callback(self.release_if_safe, root, barrier, hold)
                    HeldRuntimePublication(self.runtime, self.links, hold, intent.owners,
                                           carries).restart(comms.owners, request)
                    # Re-entering after process death has no in-memory handoff.
                    # Start only recorded, idle, stopped incarnations through Core.
                    for original in intent.owners:
                        thread = comms.registry.snapshot().threads[original.name]
                        if not thread.process_alive:
                            thread.turn_record.read().require_idle()
                            result = comms.owners.start(original.name)
                            print(f'Resumed {result.thread}: {result.pid}', flush=True)
                intent_path.unlink()
                return tuple(original.name for original in intent.owners)
            finally:
                self.release_if_safe(root, barrier, hold)

    def backup(self, root: Path) -> StoreBackup:
        key = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()
        path = Path.home() / '.local/state/agent-comms/publications' / key / self.runtime.name
        return StoreBackup(root, active_route_path(), path)

    def published(self) -> bool:
        return StoppedOwnerInstallation.current_pointer().resolve(strict=True) == self.runtime.resolve()

    def unfinished_backup(self, root: Path) -> StoreBackup | None:
        backup = self.backup(root)
        return backup if not self.published() and backup.path.exists() else None

    def restore_backup(self, backup: StoreBackup | None) -> None:
        if backup is not None and not self.published():
            backup.restore()
            backup.retire()

    def release_if_safe(self, root: Path, barrier, hold: MaintenanceState) -> None:
        if barrier.read() != hold:
            return
        backup = self.unfinished_backup(root)
        if backup is not None:
            if not backup.original():
                return
            backup.retire()
        barrier.clear(hold)

    @contextmanager
    def selected_route(self, root: Path):
        path = active_route_path()
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            acquire_exclusive_route_lock(descriptor)
            route = read_active_route(path)
            if route is None or route.root != root.resolve():
                raise ValueError('Active route changed during publication')
            yield
        finally:
            os.close(descriptor)

    def publish_links(self) -> None:
        """Move declarations and the pointer, without killing unrelated processes."""
        current = StoppedOwnerInstallation.current_pointer()
        if self.published():
            raise ValueError(f'Publication would retire the runtime it publishes: {self.runtime}')
        for link, target in self.links.items():
            replace_link(link, current / target.relative_to(self.runtime))
        replace_link(current, self.runtime)


@dataclass(frozen=True)
class HeldRuntimePublication(RuntimePublication, MaintainedOwnerCutover):
    """Existing cutover behavior owns fencing, stopped writer proof and launches."""
    maintenance_hold: MaintenanceState
    selection: tuple[OwnerRestartSelection, ...]
    carries: PreparedCarries

    def require_selection(self, snapshot, owners) -> None:
        expected = {original.name: original for original in self.selection}
        for thread in owners:
            original = expected[thread.name]
            snapshot.require_owner_process(original.identity, original.process)

    def complete(self, stopped: StoppedOwnerBatch):
        root = stopped.lifecycle.root
        backup = None
        if self.carries.conversions:
            destination = self.backup(root)
            backup = StoreBackup.take(root, destination.route, self.carries.files, destination.path)
            print(f'Backed up selected stores: {backup.path}', flush=True)
        try:
            # owner_restart.py:267 enters OwnerLifecycle.restart_wire(), retained
            # in stopped.ownership until this complete() returns or launches.
            self.carries.apply(root)
            # The CLI borrows the active route shared. Run it before the exclusive
            # pointer switch, while the stopped batch still holds the wire lock.
            self.require_cli(root, 'status')
            with self.selected_route(root):
                if not self.published():
                    self.publish_links()
        except BaseException:
            with self.selected_route(root):
                self.restore_backup(backup)
            raise
        stopped.lifecycle.maintenance.clear_stopped(self.maintenance_hold, stopped.wire_descriptor)
        return stopped.launch(LaunchRuntime(self.runtime / 'bin/python'))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    options = parser.parse_args()
    if not options.check:
        parser.error('Publish through the qualified tools/build_runtime.py --publish-runtime entrypoint')
    publication = RuntimePublication.installed()
    route = read_active_route()
    if route is None:
        raise ValueError('Runtime publication requires the installed route')
    publication.require_cli(route.root)
    owners = OwnerRestartRequest().threads(wire(route).registry.snapshot())
    print(f'{publication.runtime}: {len(owners)} native workers selected for idle publication')


if __name__ == '__main__':
    main()
