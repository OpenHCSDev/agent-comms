"""What `agent-comms install` writes for each coding harness found on this machine.

Each harness declares where it reads skills and what it needs to reach the bus;
runtime publication links and configures it, and install registers its bus threads.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from abc import abstractmethod
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import ClassVar, Self

from agent_comms_hooks import ClaudeHookProgram

from .child_process import Platform
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import ScratchName
from .host_services import SystemdUserService
from .host_services.codex_inbox import CodexInboxService


def replace_link(link: Path, target: Path) -> None:
    """Point `link` at `target` atomically; a regular file there is someone's own and is refused."""
    if link.exists() and not link.is_symlink():
        raise FileExistsError(f"Install cannot replace a regular file: {link}")
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.{ScratchName.mint()}")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
        Platform.current().sync_directory(link.parent)
    finally:
        temporary.unlink(missing_ok=True)


def merge_json(path: Path, merge) -> None:
    """Rewrite a harness's JSON config through its owner, keeping unrelated settings."""
    document = json.loads(path.read_text()) if path.exists() else {}
    merge(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n")


@dataclass(frozen=True)
class Harness(DeclaredFamily, affix="Harness"):
    home: Path

    executable: ClassVar[str]
    skills_dir: ClassVar[str]
    # External threads install registers for this harness; a harness that registers itself has none.
    bus_threads: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def present(cls, home: Path) -> tuple[Self, ...]:
        return tuple(
            member(home) for member in cls.members_with(cls) if shutil.which(member.executable)
        )

    @classmethod
    def retire_session(cls, session: str) -> SystemdUserService:
        """Stop the bridge delivering to ``session`` before its thread moves to native Pi."""
        raise RelationViolationError(
            f"{cls.declared_name} sessions have no declared bridge to retire; re-home refused.")

    @staticmethod
    def bin(name: str) -> Path:
        return Path(sys.executable).parent / name

    def links(self) -> dict[Path, Path]:
        """Every packaged skill, linked where this harness looks for skills."""
        skills = Path(str(files("agent_comms") / "skills"))
        return {
            self.home / self.skills_dir / skill.name: skill
            for skill in skills.iterdir()
            if skill.is_dir()
        }

    @abstractmethod
    def configure(self) -> tuple[Path, ...]:
        """Write this harness's own config in its own format; returns the files written."""

    @classmethod
    def published_links(cls, home: Path) -> dict[Path, Path]:
        """Refresh present harnesses and retain links of previously installed harnesses."""
        available = {type(harness) for harness in cls.present(home)}
        return {
            link: target
            for member in cls.members_with(cls)
            for link, target in member(home).links().items()
            if member in available or link.is_symlink()
        }


class ConnectorClientHarness(Harness):
    """A harness whose MCP client can reach the connector as its bus threads."""

    @abstractmethod
    def connector_registered(self) -> bool: ...

    @abstractmethod
    def register_connector(self, url: str, token: str) -> None: ...


@dataclass(frozen=True)
class ClaudeCodeHarness(ConnectorClientHarness):
    """Claude Code reads the bus through a Monitor on `agent-comms-claude-bridge watch`;
    no config holds it."""

    executable: ClassVar[str] = "claude"
    skills_dir: ClassVar[str] = ".claude/skills"
    bus_threads: ClassVar[tuple[str, ...]] = ("claude-code",)

    def connector_registered(self) -> bool:
        return subprocess.run(
            (self.executable, "mcp", "get", "comms"), capture_output=True,
        ).returncode == 0

    def register_connector(self, url: str, token: str) -> None:
        if self.connector_registered():
            subprocess.run(
                (self.executable, "mcp", "remove", "--scope", "user", "comms"), check=True,
            )
        subprocess.run(
            (self.executable, "mcp", "add-json", "--scope", "user", "comms", json.dumps({
                "type": "http", "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            })),
            check=True,
        )

    def configure(self) -> tuple[Path, ...]:
        settings = self.home / ".claude/settings.json"

        def reconcile_hooks(document: dict) -> None:
            events = document.setdefault("hooks", {})
            for event, entries in events.items():
                events[event] = [
                    entry for entry in entries if not ClaudeHookProgram.owns_entry(entry)
                ]
            for program in ClaudeHookProgram.members():
                for event in program.events:
                    events.setdefault(event, []).append(program.entry())

        merge_json(settings, reconcile_hooks)
        return (settings, self.home / ".claude.json")


@dataclass(frozen=True)
class CodexHarness(Harness):
    """Codex is pushed to by a user service; the user starts it after binding a conversation."""

    executable: ClassVar[str] = "codex"
    skills_dir: ClassVar[str] = ".codex/skills"

    def configure(self) -> tuple[Path, ...]:
        CodexInboxService.install(home=self.home)
        return (CodexInboxService.unit_file(self.home),)

    @classmethod
    def retire_session(cls, session: str) -> CodexInboxService:
        if not session:
            raise RelationViolationError("Codex reported no session id; cannot retire its inbox.")
        inbox = CodexInboxService(session)
        inbox.retire()
        return inbox


@dataclass(frozen=True)
class OpenCodeHarness(Harness, declared_name="opencode"):
    """OpenCode loads the TUI plugin, which attaches its session to the bus itself;
    run `opencode-comms`."""

    executable: ClassVar[str] = "opencode"
    skills_dir: ClassVar[str] = ".config/opencode/skills"

    @property
    def plugin(self) -> Path:
        return self.home / ".local/share/agent-comms/opencode_tui.ts"

    def links(self) -> dict[Path, Path]:
        return super().links() | {
            self.plugin: Path(str(files("agent_comms.cli_bridges") / "opencode_tui.ts"))
        }

    def configure(self) -> tuple[Path, ...]:
        tui = self.home / ".config/opencode/tui.json"
        url = self.plugin.as_uri()

        def add_plugin(document: dict) -> None:
            plugins = document.setdefault("plugin", [])
            if url not in plugins:
                plugins.append(url)

        merge_json(tui, add_plugin)
        return (tui,)
