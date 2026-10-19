"""Core's idle publication transaction, called by the qualified runtime builder.

Core owns maintenance, exact worker selection, stopped cutover and recovery.
The frontend supplies a context owning its readers; it enters only after drain.
"""
from __future__ import annotations

import argparse
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field
from abc import abstractmethod
import hashlib
from io import StringIO
from importlib.metadata import distribution
import json
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.active_route import read_active_route, active_route_path, acquire_exclusive_route_lock
from agent_comms.comms import wire
from agent_comms.cli_commands import StatusCliCommand
from agent_comms.field_codec import FieldCodec, SubclassUnion, NullMember, RecordMember
from agent_comms.harness_install import Harness, replace_link
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.maintenance_barrier import MaintenanceOperator, MaintenanceState
from agent_comms.owner_cutover import StoppedOwnerInstallation, MaintainedOwnerCutover
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.owner_restart import OwnerRestartRequest, StoppedOwnerBatch
from agent_comms.store_files import _atomic_write_text, _store_lock
from .cutover.carry import PreparedCarries
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
        carries = PreparedCarries.at(checkout, root, self.backup(root), self.published())
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
                snapshot = comms.registry.snapshot()
                selected = OwnerRestartRequest().threads(snapshot)
                intent = intent.select(snapshot, selected, self.runtime, previous, intent_path)
                carries.publish(self, comms, intent, hold, resources, timeout)
                intent_path.unlink()
                return tuple(original.name for original in intent.owners)
            finally:
                self.release_if_safe(carries, barrier, hold)

    def publish_stopped(self, carries, comms, intent, hold, resources) -> None:
        root = comms.root
        snapshot = comms.registry.snapshot()
        remaining = [original.name for original in OwnerRestartSelection.live(intent.owners, snapshot)]
        request = OwnerRestartRequest(names=tuple(remaining),
            source_interpreter=intent.previous + '/bin/python')
        if not carries.original():
            if OwnerRestartSelection.live(intent.owners, comms.registry.snapshot()):
                raise RuntimeError('Cannot restore data while an original worker is running')
            with self.selected_route(root), _store_lock(root / 'wire'):
                carries.restore_if_unpublished(self.published, nullcontext)
        with ExitStack() as publication:
            publication.enter_context(resources)
            publication.callback(self.release_if_safe, carries, comms.owners.maintenance, hold)
            HeldRuntimePublication(self.runtime, self.links, hold, intent.owners,
                                   carries).restart(comms.owners, request)

    def backup(self, root: Path) -> StoreBackup:
        key = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()
        path = Path.home() / '.local/state/agent-comms/publications' / key / self.runtime.name
        return StoreBackup(root, active_route_path(), path)

    def published(self) -> bool:
        return StoppedOwnerInstallation.current_pointer().resolve(strict=True) == self.runtime.resolve()

    def release_if_safe(self, carries: PreparedCarries, barrier, hold: MaintenanceState) -> None:
        if barrier.read() == hold and carries.cleanup(self.published()):
            barrier.clear(hold)

    @contextmanager
    def selected_route(self, root: Path):
        path = active_route_path()
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            acquire_exclusive_route_lock(descriptor)
            print('Acquired route lock before stopped wire lock', flush=True)
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
    route_ownership: ExitStack = field(default_factory=ExitStack, compare=False)

    def stopped_resources(self, lifecycle):
        # Agent observation children hold shared route locks until they stop.
        # The restart owner calls this after stopping them and before its wire lock.
        self.route_ownership.enter_context(self.selected_route(lifecycle.root))
        return self.route_ownership

    def require_selection(self, snapshot, owners) -> None:
        expected = {original.name: original for original in self.selection}
        for thread in owners:
            original = expected[thread.name]
            snapshot.require_owner_process(original.identity, original.process)

    def complete(self, stopped: StoppedOwnerBatch):
        root = stopped.lifecycle.root
        with ExitStack() as conversion:
            carries = self.carries.capture()
            conversion.callback(carries.restore_if_unpublished, self.published, nullcontext)
            # owner_restart.py:267 enters OwnerLifecycle.restart_wire(), retained
            # in stopped.ownership until this complete() returns or launches.
            carries.apply(root)
            output = StringIO()
            if StatusCliCommand().run(wire(root), output):
                raise RuntimeError('Converted bus status failed: ' + output.getvalue())
            print('Converted bus status: ' + output.getvalue().strip(), flush=True)
            if not self.published():
                self.publish_links()
        stopped.lifecycle.maintenance.clear_stopped(self.maintenance_hold, stopped.wire_descriptor)
        self.route_ownership.close()
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
