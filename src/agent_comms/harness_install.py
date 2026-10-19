"""What `agent-comms install` writes for each coding harness found on this machine.

Each harness declares where it reads skills and what it needs to reach the bus;
install and the installer's publication of a runtime both read those declarations.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from abc import abstractmethod
from dataclasses import dataclass
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from agent_comms_hooks import ClaudeHookProgram

from .cli_result import CliResult
from .declared_family import DeclaredFamily
from .thread_execution import ExternalThreadExecution

if TYPE_CHECKING:
    from .comms import Comms


def replace_link(link: Path, target: Path) -> None:
    """Point `link` at `target` atomically; a regular file there is someone's own and is refused."""
    if link.exists() and not link.is_symlink():
        raise FileExistsError(f'Install cannot replace a regular file: {link}')
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f'.{link.name}.{uuid.uuid4().hex}')
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
        directory = os.open(link.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def merge_json(path: Path, merge) -> None:
    """Rewrite a harness's own JSON config with one addition, keeping everything else it holds."""
    document = json.loads(path.read_text()) if path.exists() else {}
    merge(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + '\n')


def installation_links(home: Path) -> dict[Path, Path]:
    """What an installation links to this interpreter's runtime: every program the Core
    and Toad distributions put in its ``bin``, and every harness's installed skill links."""
    binary = Path(sys.executable).parent
    programs = (Path(os.path.normpath(entry.locate()))
                for package in ('agent-comms', 'batrachian-toad')
                for entry in distribution(package).files or ())
    return {home / '.local/bin' / program.name: program
            for program in programs if program.parent == binary} | Harness.published_links(home)


@dataclass(frozen=True)
class HarnessInstalled:
    """What one harness's install linked, configured and registered."""
    links: tuple[str, ...]
    config: tuple[str, ...]
    registered: tuple[str, ...]


@dataclass(frozen=True)
class Harness(DeclaredFamily, affix="Harness"):
    home: Path

    executable: ClassVar[str]
    skills_dir: ClassVar[str]

    @classmethod
    def present(cls, home: Path) -> tuple[Harness, ...]:
        return tuple(member(home) for member in cls.members_with(cls) if shutil.which(member.executable))

    @staticmethod
    def bin(name: str) -> Path:
        return Path(sys.executable).parent / name

    def links(self) -> dict[Path, Path]:
        """Every packaged skill, linked where this harness looks for skills."""
        skills = Path(str(files('agent_comms') / 'skills'))
        return {self.home / self.skills_dir / skill.name: skill for skill in skills.iterdir() if skill.is_dir()}

    def bus_threads(self, commander: str) -> tuple[str, ...]:
        """External threads install registers for this harness; a harness that registers itself has none."""
        return ()

    @abstractmethod
    def configure(self) -> tuple[Path, ...]:
        """Write this harness's own config in its own format; returns the files written."""

    def install(self, comms: Comms, commander: str, project: Path) -> HarnessInstalled:
        for link, target in self.links().items():
            replace_link(link, target)
        names = comms.registry.snapshot()
        registered = [name for name in self.bus_threads(commander) if not names.taken(name)]
        for name in registered:
            comms.registry.declare(comms.registry.new_thread(name=name, tags=frozenset(), worktree=str(project),
                                                                execution=ExternalThreadExecution))
        return HarnessInstalled(tuple(sorted(map(str, self.links()))),
                                tuple(sorted(map(str, self.configure()))), tuple(registered))

    @classmethod
    def published_links(cls, home: Path) -> dict[Path, Path]:
        """Installed links of every harness, which publishing a runtime moves to that runtime."""
        return {link: target for member in cls.members_with(cls)
                for link, target in member(home).links().items() if link.is_symlink()}


@dataclass(frozen=True)
class ClaudeCodeHarness(Harness):
    """Claude Code reads the bus through a Monitor on `agent-comms-claude-bridge watch`; no config holds it."""

    executable: ClassVar[str] = 'claude'
    skills_dir: ClassVar[str] = '.claude/skills'

    def bus_threads(self, commander: str) -> tuple[str, ...]:
        return (commander,)

    def configure(self) -> tuple[Path, ...]:
        settings = self.home / '.claude/settings.json'

        def add_hooks(document: dict) -> None:
            for program in ClaudeHookProgram.members():
                for event in program.events:
                    hooks = document.setdefault('hooks', {}).setdefault(event, [])
                    commands = {hook.get('command') for entry in hooks
                                for hook in entry.get('hooks', ())}
                    if program.command() not in commands:
                        hooks.append(program.entry())
        merge_json(settings, add_hooks)
        return (settings,)


@dataclass(frozen=True)
class CodexHarness(Harness):
    """Codex is pushed to by a user service; the user starts it after binding a conversation."""

    executable: ClassVar[str] = 'codex'
    skills_dir: ClassVar[str] = '.codex/skills'

    def bus_threads(self, commander: str) -> tuple[str, ...]:
        return ('codex',)

    def configure(self) -> tuple[Path, ...]:
        unit = self.home / '.config/systemd/user/codex-comms-inbox.service'
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(f"""[Unit]
Description=Core message stream to the selected Codex conversation

[Service]
Type=simple
ExecStart={self.bin('agent-comms-codex-bridge')} watch --state {self.home / '.local/state/codex-comms-inbox'}
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=10
UMask=0077
""")
        return (unit,)


@dataclass(frozen=True)
class OpenCodeHarness(Harness, declared_name="opencode"):
    """OpenCode loads the TUI plugin, which attaches its session to the bus itself; run `opencode-comms`."""

    executable: ClassVar[str] = 'opencode'
    skills_dir: ClassVar[str] = '.config/opencode/skills'

    @property
    def plugin(self) -> Path:
        return self.home / '.local/share/agent-comms/opencode_tui.ts'

    def links(self) -> dict[Path, Path]:
        return super().links() | {self.plugin: Path(str(files('agent_comms.cli_bridges') / 'opencode_tui.ts'))}

    def configure(self) -> tuple[Path, ...]:
        tui = self.home / '.config/opencode/tui.json'
        url = self.plugin.as_uri()

        def add_plugin(document: dict) -> None:
            plugins = document.setdefault('plugin', [])
            if url not in plugins:
                plugins.append(url)
        merge_json(tui, add_plugin)
        return (tui,)
