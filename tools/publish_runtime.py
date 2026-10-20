"""Core's idle publication transaction, called by the qualified runtime builder.

Core owns maintenance, exact worker selection, stopped cutover and recovery.
The frontend supplies a context owning its readers; it enters only after drain.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from abc import abstractmethod
from contextlib import (
    AbstractContextManager,
    ExitStack,
    contextmanager,
    nullcontext,
    redirect_stdout,
)
from dataclasses import dataclass, field, replace
from importlib.metadata import distribution
from io import StringIO
from pathlib import Path
from typing import Annotated

from cutover.backup import StoreBackup
from cutover.carry import PendingConversion, PreparedCarries
from cutover.runtime_resolution import RuntimeResolution

import agent_comms
from agent_comms.active_route import (
    acquire_exclusive_route_lock,
    active_route_path,
    read_active_route,
)
from agent_comms.cli_commands import StatusCliCommand
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec, NullMember, PathText, RecordMember, SubclassUnion
from agent_comms.harness_install import Harness, replace_link
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.maintenance_barrier import MaintenanceOperator, MaintenanceState
from agent_comms.owner_cutover import MaintainedOwnerCutover
from agent_comms.owner_lifecycle import OwnerRestartResult, OwnerRestartSelection, OwnerStartResult
from agent_comms.owner_restart import OwnerRestartHandoff, OwnerRestartRequest, StoppedOwnerBatch
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.store_files import _atomic_write_text, _store_lock
from agent_comms.threads import Thread


class PublicationIntent(SubclassUnion, affix=''):
    """Retain stopped witnesses and include owners running when publication resumes."""

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

    @staticmethod
    def capture(snapshot, selected, runtime: Path, previous: Path, path: Path,
                owners: tuple[OwnerRestartSelection, ...] = ()) -> SavedPublicationIntent:
        names = {owner.name for owner in owners}
        intent = SavedPublicationIntent(str(runtime), str(previous), owners + tuple(
            OwnerRestartSelection.capture(snapshot, thread.name)
            for thread in selected if thread.name not in names))
        _atomic_write_text(path, json.dumps(FieldCodec.encode(intent)) + '\n', fsync_parent=True)
        return intent


class NoPublicationIntent(NullMember, PublicationIntent):
    def require_target(self, runtime: Path, previous: Path) -> None:
        if previous == runtime:
            raise ValueError('Runtime is already current')

    def select(self, snapshot, selected, runtime: Path, previous: Path,
               path: Path) -> SavedPublicationIntent:
        return self.capture(snapshot, selected, runtime, previous, path)


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
        return self.capture(snapshot, selected, Path(self.runtime), Path(self.previous),
                            path, self.owners)


@dataclass(frozen=True)
class RuntimePublication:
    runtime: Annotated[Path, PathText]
    links: dict[Annotated[Path, PathText], Annotated[Path, PathText]]

    @classmethod
    def installed(cls) -> RuntimePublication:
        binary = Path(sys.executable).parent.resolve(strict=True)
        if not Path(agent_comms.__file__).resolve().is_relative_to(binary.parent):
            raise ValueError("Editable Core cannot publish a runtime; build the selected source")
        RuntimeResolution.at(binary.parent)
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
        installed = LaunchRuntime.current_pointer().resolve(strict=True)
        if Path(sys.executable).parent.resolve().parent != installed:
            raise RuntimeError('Drain and stop must run with the installed source interpreter')
        carries = PreparedCarries.at(checkout, root, self.backup(root), self.published())
        self.require_cli(root)
        path = active_route_path()
        route = read_active_route(path)
        if route is None or route.root != root.resolve():
            raise ValueError('Publication requires the selected active root')
        comms = wire(route)
        barrier = comms.owners.maintenance
        operator = MaintenanceOperator.current()
        intent_path = root / 'runtime-publication.json'
        with _store_lock(root / 'runtime-publication'):
            previous = LaunchRuntime.current_pointer().resolve(strict=True)
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
        request = OwnerRestartRequest(names=tuple(original.name for original in intent.owners))
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
        return LaunchRuntime.current_pointer().resolve(strict=True) == self.runtime.resolve()

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
            yield descriptor
        finally:
            os.close(descriptor)

    def published_target(self, current: Path, target: Path) -> Path:
        """Runtime-owned links follow current; user-owned paths remain where declared."""
        if target.is_relative_to(self.runtime):
            return current / target.relative_to(self.runtime)
        return target

    def publish_links(self) -> None:
        """Refresh the declared installation, including an already-current runtime."""
        current = LaunchRuntime.current_pointer()
        selected = self.runtime.resolve(strict=True)
        RuntimeResolution.at(selected)
        for link, target in self.links.items():
            replace_link(link, self.published_target(current, target))
        replace_link(current, selected)
        for harness in Harness.present(Path.home()):
            harness.configure()


@dataclass(frozen=True)
class HeldRuntimePublication(RuntimePublication, MaintainedOwnerCutover):
    """Existing cutover behavior owns fencing, stopped writer proof and launches."""
    maintenance_hold: MaintenanceState
    selection: tuple[OwnerRestartSelection, ...]
    carries: PendingConversion
    route_ownership: ExitStack = field(default_factory=ExitStack, compare=False)
    route_descriptor: int = field(init=False)

    def restart(self, lifecycle, request) -> tuple[OwnerRestartResult, ...]:
        snapshot = lifecycle.registry.snapshot()
        running = replace(request, names=tuple(
            thread.name for thread in request.threads(snapshot)
            if thread.restart_candidates(snapshot.status(thread.name))))
        results = super().restart(lifecycle, running)
        # A previous failed cutover already retired these owners. Their /proc
        # environment is gone; normal start derives their declared launch anew.
        # The stopped batch has released its wire/route locks before this step.
        for original in self.selection:
            thread = lifecycle.registry.require(original.name)
            if thread.incarnation != original.identity.incarnation:
                raise ValueError('Saved publication owner incarnation changed')
            if not thread.process_alive:
                completed = subprocess.run([
                    self.runtime / 'bin/agent-comms', '--root', str(lifecycle.root),
                    'start', '--name', original.name,
                ], capture_output=True, text=True, check=True, timeout=60)
                started = FieldCodec.decode(OwnerStartResult, FieldCodec.parse(completed.stdout))
                results += (OwnerRestartResult(original.name, original.process.pid, started.pid),)
        return results

    def stopped_resources(self, lifecycle):
        # Agent observation children hold shared route locks until they stop.
        # The restart owner calls this after stopping them and before its wire lock.
        descriptor = self.route_ownership.enter_context(self.selected_route(lifecycle.root))
        object.__setattr__(self, 'route_descriptor', descriptor)
        return self.route_ownership

    def owner_selection(
        self, snapshot: RegistrySnapshot, owner: Thread, request: OwnerRestartRequest,
    ) -> OwnerRestartSelection:
        return next(original for original in self.selection if original.name == owner.name)

    def require_selection(self, snapshot, owners) -> None:
        for thread in owners:
            original = next(original for original in self.selection if original.name == thread.name)
            original.require_current(snapshot)

    def complete(self, stopped: StoppedOwnerBatch) -> tuple[OwnerRestartResult, ...]:
        request = StoppedPublication(RuntimePublication(self.runtime, self.links),
                                     stopped.handoff, self.maintenance_hold,
                                     self.carries.checkout)
        result = subprocess.run([
            self.runtime / 'bin/python', '-E', '-s', '-B',
            str(request.checkout / Path(__file__).parent.name / Path(__file__).name),
            '--complete-stopped',
            str(stopped.wire_descriptor), str(self.route_descriptor),
        ], input=json.dumps(FieldCodec.encode(request)), text=True, stdout=subprocess.PIPE,
            pass_fds=(stopped.wire_descriptor, self.route_descriptor), check=True)
        return FieldCodec.decode(tuple[OwnerRestartResult, ...], FieldCodec.parse(result.stdout))


@dataclass(frozen=True)
class StoppedPublication:
    """The original stopped handoff crosses into the selected target interpreter."""

    publication: RuntimePublication
    handoff: OwnerRestartHandoff
    maintenance_hold: MaintenanceState
    checkout: Annotated[Path, PathText]

    def complete(self, wire_descriptor: int, route_descriptor: int) -> tuple[OwnerRestartResult, ...]:
        root = Path(self.handoff.root)
        publication = self.publication
        carries = PreparedCarries.at(self.checkout, root, publication.backup(root),
                                    publication.published()).capture()
        with ExitStack() as conversion:
            conversion.callback(carries.restore_if_unpublished, publication.published, nullcontext)
            carries.apply(root)
            comms = wire(root)
            output = StringIO()
            if StatusCliCommand().run(comms, output):
                raise RuntimeError('Converted bus status failed: ' + output.getvalue())
            print('Converted bus status: ' + output.getvalue().strip(), flush=True)
            publication.publish_links()
        stopped = StoppedOwnerBatch.accept(comms.owners, wire_descriptor, self.handoff)
        comms.owners.maintenance.clear_stopped(self.maintenance_hold, wire_descriptor)
        fcntl.flock(route_descriptor, fcntl.LOCK_UN)
        return stopped.launch(LaunchRuntime(publication.runtime / 'bin/python'))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--check', action='store_true')
    actions.add_argument('--refresh', action='store_true',
                         help='Refresh this installed runtime\'s declared launchers and harness config')
    actions.add_argument('--complete-stopped', type=int, nargs=2, metavar=('WIRE_FD', 'ROUTE_FD'))
    options = parser.parse_args()
    if options.complete_stopped is not None:
        request = FieldCodec.decode(StoppedPublication, FieldCodec.parse(sys.stdin.read()))
        with redirect_stdout(sys.stderr):
            result = request.complete(*options.complete_stopped)
        print(json.dumps(FieldCodec.encode(result)))
        return
    publication = RuntimePublication.installed()
    if options.refresh:
        publication.publish_links()
        return
    route = read_active_route()
    if route is None:
        raise ValueError('Runtime publication requires the installed route')
    publication.require_cli(route.root)
    owners = OwnerRestartRequest().threads(wire(route).registry.snapshot())
    print(f'{publication.runtime}: {len(owners)} native workers selected for idle publication')


if __name__ == '__main__':
    main()
