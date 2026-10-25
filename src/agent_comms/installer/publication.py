"""Select the interpreter running this module as the published runtime.

``PublicationProbe`` runs it in the runtime the installer built: console launchers and
harness links move to it, then ``current`` does.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib.metadata import distribution
from pathlib import Path
from typing import Annotated

import agent_comms
from agent_comms.connector import LOOPBACK, PORT
from agent_comms.connector_store import ConnectorStore
from agent_comms.field_codec import PathText
from agent_comms.harness_install import ConnectorClientHarness, Harness, replace_link
from agent_comms.installer.runtime_resolution import (
    ConsoleScriptPackage,
    RuntimePackage,
    RuntimeResolution,
)
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
        packages = tuple(
            member.distribution for member in RuntimePackage.members_with(ConsoleScriptPackage))
        links = {
            Path.home() / '.local/bin' / entry.name: binary / entry.name
            for package in packages
            for entry in distribution(package).entry_points.select(group='console_scripts')
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
        store = ConnectorStore.owned()
        for client in ConnectorClientHarness.present(Path.home()):
            for name in client.bus_threads:
                if not store.read().thread_held(name) or not client.connector_registered():
                    token = store.thread_granted(name)
                    client.register_connector(f"http://{LOOPBACK}:{PORT}/mcp", token)
