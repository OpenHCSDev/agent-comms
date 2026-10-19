"""Publish a reviewed, wire-compatible runtime and queue idle worker reloads.

Run with the target runtime's Python. The caller must review protocol and store
compatibility first: this command performs no schema migration. Incompatible
changes belong to the existing stopped-owner installation workflow.

Command names come from installed package declarations. Publication records the
new installation; ordinary launches keep their caller's runtime. Existing owners
retain their code until Core's restart queue can stop them while idle.
"""
from __future__ import annotations

import argparse
from contextlib import suppress
from dataclasses import dataclass
from importlib.metadata import distribution
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import ClassVar

from agent_comms.active_route import read_active_route, active_route_path, acquire_exclusive_route_lock
from agent_comms.child_process import LinuxPlatform, ProcessIdentity, ProcessState
from agent_comms.comms import wire
from agent_comms.harness_install import Harness, replace_link
from agent_comms.owner_restart import OwnerRestartRequest
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.restart_queue import enqueue


@dataclass(frozen=True)
class RunningProcess:
    """One /proc entry: its exact identity, its parent, and the executables it started from.

    A venv's bin/python resolves to the base interpreter, so the launched argv[0] counts with only
    its directory resolved; /proc/<pid>/exe counts when this user may read it."""

    identity: ProcessIdentity
    parent: int
    executables: tuple[Path, ...]

    @classmethod
    def all(cls) -> tuple[RunningProcess, ...]:
        """Every process, read once from /proc; one that exits during the read is left out."""
        running = []
        for entry in Path('/proc').iterdir():
            if entry.name.isdecimal():
                with suppress(FileNotFoundError, ProcessLookupError):
                    running.append(cls.read(entry))
        return tuple(running)

    @classmethod
    def read(cls, entry: Path) -> RunningProcess:
        # Fields after comm's final ')' start at field 3: ppid is 4, pgrp 5, starttime 22.
        fields = (entry / 'stat').read_text().rsplit(') ', 1)[1].split()
        if fields[0] in {'Z', 'X'}:
            raise ProcessLookupError(entry.name)
        argv0 = Path(os.fsdecode((entry / 'cmdline').read_bytes().split(b'\0', 1)[0]))
        launched: tuple[Path, ...] = ((argv0.parent.resolve() / argv0.name,)
                                      if argv0.is_absolute() else ())
        with suppress(PermissionError):
            launched += (Path(os.readlink(entry / 'exe')),)
        identity = ProcessIdentity.in_group(int(entry.name), int(fields[19]), int(fields[2]))
        return cls(identity, int(fields[1]), launched)

    def runs_from(self, runtime: Path) -> bool:
        return any(path.is_relative_to(runtime) for path in self.executables)


@dataclass(frozen=True)
class RetiredRuntime:
    """The runtime a publication replaced. Its processes end once the links point at the new one,
    Toad views included, except a registered owner still waiting for its idle reload, that
    owner's descendants, and the publisher's own ancestors."""

    path: Path
    grace: ClassVar[float] = 5.0

    def processes(self, owners: tuple[ProcessState, ...]) -> tuple[RunningProcess, ...]:
        running = {process.identity.pid: process for process in RunningProcess.all()}

        def lineage(pid: int) -> tuple[ProcessIdentity, ...]:
            line: dict[int, ProcessIdentity] = {}
            while pid in running and pid not in line:
                line[pid] = running[pid].identity
                pid = running[pid].parent
            return tuple(line.values())

        publisher = lineage(os.getpid())
        return tuple(process for process in running.values()
                     if process.runs_from(self.path) and process.identity not in publisher
                     and not any(identity in owners for identity in lineage(process.identity.pid)))

    def end(self, owners: tuple[ProcessState, ...]) -> tuple[RunningProcess, ...]:
        """SIGTERM each exact process, one shared wait, then SIGKILL any still running."""
        platform = LinuxPlatform()
        signalled: list[tuple[RunningProcess, int]] = []
        for process in self.processes(owners):
            with suppress(ProcessLookupError):
                descriptor = platform.open_pidfd(process.identity.pid)
                try:
                    platform.require(process.identity)
                    platform.signal_pidfd(descriptor, signal.SIGTERM)
                except ProcessLookupError:
                    os.close(descriptor)
                    continue
                signalled.append((process, descriptor))
        deadline = time.monotonic() + self.grace
        for _, descriptor in signalled:
            try:
                platform.watch_deadline(descriptor, deadline)
            finally:
                os.close(descriptor)
        return tuple(process for process, _ in signalled)


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

    def require_cli(self, root: Path) -> None:
        result = subprocess.run(
            [self.runtime / 'bin/agent-comms', '--root', str(root), 'status'],
            capture_output=True, text=True,
        )
        if result.returncode or result.stderr:
            raise RuntimeError(
                f'Runtime CLI refused publication (exit {result.returncode}): {result.stdout}{result.stderr}'
            )

    def publish(self, root: Path, owners: tuple[ProcessState, ...]) -> tuple[RunningProcess, ...]:
        """Publish, then end what the retired runtime still runs apart from waiting owners."""
        self.require_cli(root)
        path = active_route_path()
        previous_route = read_active_route(path)
        if previous_route is None or previous_route.root != root.resolve():
            raise ValueError('Publication requires the selected active root')
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            acquire_exclusive_route_lock(directory)
            if read_active_route(path) != previous_route:
                raise ValueError('Active route changed during CLI check')
            return self.publish_links(owners)
        finally:
            os.close(directory)

    def publish_links(self, owners: tuple[ProcessState, ...]) -> tuple[RunningProcess, ...]:
        current = StoppedOwnerInstallation.current_pointer()
        if not current.is_symlink():
            if current.exists():
                raise FileExistsError(current)
            previous = (Path.home() / '.local/bin/toad').resolve(strict=True).parent.parent
            # Initial conversion leaves every command on the same old runtime.
            # A process exit midway therefore leaves usable, coherent links.
            for link, target in self.links.items():
                if link.resolve(strict=True) != previous / target.relative_to(self.runtime):
                    raise ValueError(f'Existing runtime links disagree: {link}')
            replace_link(current, previous)
        retired = RetiredRuntime(current.resolve(strict=True))
        if retired.path == self.runtime.resolve():
            raise ValueError(f'Publication would retire the runtime it publishes: {self.runtime}')
        for link, target in self.links.items():
            replace_link(link, current / target.relative_to(self.runtime))
        replace_link(current, self.runtime)
        return retired.end(owners)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Inspect publication without changing links or queueing restarts')
    options = parser.parse_args()
    publication = RuntimePublication.installed()
    route = read_active_route()
    if route is None:
        raise ValueError('Runtime publication requires the installed route')
    comms = wire(route)
    snapshot = comms.registry.snapshot()
    owners = tuple(OwnerRestartRequest().threads(snapshot))
    if options.check:
        publication.require_cli(route.root)
        print(f'{publication.runtime}: {len(owners)} native workers would be queued for idle reload')
        return
    registered = tuple(thread.process_identity for thread in snapshot.threads.values())
    ended = publication.publish(route.root, registered)
    print(f'Published {publication.runtime}', flush=True)
    for process in ended:
        print(f'Ended {process.identity.pid} from the retired runtime: {process.executables[0]}',
              flush=True)
    # Queue requests keep original process identity and session settings. A
    # pending or uncertain previous request is handled by that existing owner.
    os.environ['VIRTUAL_ENV'] = str(publication.runtime)
    selected = LaunchRuntime(publication.runtime / 'bin/python')
    for owner in owners:
        request = enqueue(comms, owner.name, launch_runtime=selected)
        print(f'{owner.name}: {request.state.declared_name} ({request.id})', flush=True)


if __name__ == '__main__':
    main()
