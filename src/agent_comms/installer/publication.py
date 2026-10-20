"""Select the interpreter running this module as the published runtime.

The installer runs ``python -m agent_comms.installer.publication`` with the runtime
it built: console launchers and harness links move to it, then ``current`` does.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib.metadata import distribution
from pathlib import Path
from typing import Annotated

import agent_comms
from agent_comms.field_codec import PathText
from agent_comms.harness_install import Harness, replace_link
from agent_comms.installer.runtime_resolution import RuntimeResolution
from agent_comms.launch_runtime import LaunchRuntime


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


if __name__ == '__main__':
    RuntimePublication.installed().publish_links()
