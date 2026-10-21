"""Who runs one install and how far it goes, decided once where the command is read."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import BoundedText, SubclassUnion
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_text import AbsentText, GivenText, TextChoice

if TYPE_CHECKING:
    from agent_comms.cli_bridges.shared import SessionBridgeResumes
    from agent_comms.cli_result import CliResult
    from agent_comms.installer.upgrade import Install, RehearsedUpgrade, Upgrade


class InstallerChoice(SubclassUnion, TextChoice, affix="Installer"):
    """The unit running this install, or none."""

    @abstractmethod
    def scratch(self, rule: StorageRule) -> Path:
        """Where this install keeps its worktree and practice copies."""


@dataclass(frozen=True, slots=True)
class NamedInstaller(GivenText, InstallerChoice):
    text: Annotated[str, BoundedText]

    def scratch(self, rule: StorageRule) -> Path:
        return rule.initial_roots(self.text).scratch


@dataclass(frozen=True, slots=True)
class UnnamedInstaller(AbsentText, InstallerChoice):
    def scratch(self, rule: StorageRule) -> Path:
        return rule.scratch_base


class ReportDestination(ABC):
    """Where one install run posts its start and its result."""

    @abstractmethod
    def post(self, lines: tuple[str, ...]) -> None: ...


class Unposted(ReportDestination):
    """No bus takes the post: a rehearsal changes nothing on it, and a fresh root has none
    before its install creates one. The run's own output carries the result."""

    def post(self, lines: tuple[str, ...]) -> None:
        pass


class RunMode(DeclaredFamily, affix="Mode"):
    """Whether an install only rehearses on a copy of the bus or goes on to change it."""

    @abstractmethod
    def destination(self, live: ReportDestination) -> ReportDestination:
        """Where this run posts, given where its install kind posts when it changes the bus."""

    @abstractmethod
    def finish(self, install: Install, rehearsed: RehearsedUpgrade,
               bridges: SessionBridgeResumes, harnesses: Callable[[], CliResult]) -> Upgrade:
        """The run's result once its rehearsal passed: where the run stops."""


@dataclass(frozen=True)
class RehearsalMode(RunMode):
    """``--rehearse``: carry and read a copy of the bus and stop there."""

    def destination(self, live: ReportDestination) -> ReportDestination:
        return Unposted()

    def finish(self, install: Install, rehearsed: RehearsedUpgrade,
               bridges: SessionBridgeResumes, harnesses: Callable[[], CliResult]) -> Upgrade:
        return rehearsed


@dataclass(frozen=True)
class LiveMode(RunMode):
    """Install on the live bus after the rehearsal passed, then register the harnesses."""

    def destination(self, live: ReportDestination) -> ReportDestination:
        return live

    def finish(self, install: Install, rehearsed: RehearsedUpgrade,
               bridges: SessionBridgeResumes, harnesses: Callable[[], CliResult]) -> Upgrade:
        return install.finished(rehearsed, bridges, harnesses)
