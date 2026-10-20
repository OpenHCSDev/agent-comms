"""Installation link declarations used only by tools/upgrade_bus.py.

There is no command-line installer here. Upgrade owns rehearsal, stopping,
conversion, runtime-state reset and restarting the original agent set.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import distribution
from pathlib import Path
import sys
from typing import Annotated

from agent_comms.field_codec import PathText
from agent_comms.harness_install import Harness, replace_link
from agent_comms.launch_runtime import LaunchRuntime


@dataclass(frozen=True)
class RuntimePublication:
    runtime: Annotated[Path, PathText]
    links: dict[Annotated[Path, PathText], Annotated[Path, PathText]]

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

    def published(self) -> bool:
        return LaunchRuntime.current_pointer().resolve(strict=True) == self.runtime.resolve()

    def publish_links(self) -> None:
        """Refresh declarations only after upgrade has stopped its agent set."""
        current = LaunchRuntime.current_pointer()
        selected = self.runtime.resolve(strict=True)
        for link, target in self.links.items():
            replace_link(link, current / target.relative_to(self.runtime))
        replace_link(current, selected)
        for harness in Harness.present(Path.home()):
            harness.configure()
