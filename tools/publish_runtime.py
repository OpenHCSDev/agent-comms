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
from dataclasses import dataclass
from importlib.metadata import distribution
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.active_route import read_active_route, active_route_path, acquire_exclusive_route_lock
from agent_comms.comms import wire
from agent_comms.harness_install import Harness, replace_link
from agent_comms.owner_restart import OwnerRestartRequest
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.restart_queue import enqueue


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

    def publish(self, root: Path) -> None:
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
            self.publish_links()
        finally:
            os.close(directory)

    def publish_links(self) -> None:
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
        for link, target in self.links.items():
            replace_link(link, current / target.relative_to(self.runtime))
        replace_link(current, self.runtime)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Inspect publication without changing links or queueing restarts')
    options = parser.parse_args()
    publication = RuntimePublication.installed()
    route = read_active_route()
    if route is None:
        raise ValueError('Runtime publication requires the installed route')
    comms = wire(route)
    owners = tuple(OwnerRestartRequest().threads(comms.registry.snapshot()))
    if options.check:
        publication.require_cli(route.root)
        print(f'{publication.runtime}: {len(owners)} native workers would be queued for idle reload')
        return
    publication.publish(route.root)
    print(f'Published {publication.runtime}', flush=True)
    # Queue requests keep original process identity and session settings. A
    # pending or uncertain previous request is handled by that existing owner.
    os.environ['VIRTUAL_ENV'] = str(publication.runtime)
    selected = LaunchRuntime(publication.runtime / 'bin/python')
    for owner in owners:
        request = enqueue(comms, owner.name, launch_runtime=selected)
        print(f'{owner.name}: {request.state.declared_name} ({request.id})', flush=True)


if __name__ == '__main__':
    main()
