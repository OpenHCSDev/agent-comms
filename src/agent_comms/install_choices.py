"""Who runs one install, selecting its scratch directory without scheduling another."""

from __future__ import annotations

from abc import abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Literal

from agent_comms.field_codec import BoundedText, SingleFieldValue, SubclassUnion
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_text import AbsentText, GivenText, TextChoice

if TYPE_CHECKING:
    from agent_comms.cli_bridges.shared import SessionBridgeResumes
    from agent_comms.installer.probes.harnesses import InstallResult
    from agent_comms.installer.launch import InstallReport
    from agent_comms.installer.reporting import RootWriter
    from agent_comms.installer.upgrade import (
        Install,
        OwnersStopped,
        Rehearsal,
        RehearsedUpgrade,
        Stack,
    )


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


class RunMode(SubclassUnion, affix="Mode"):
    """The command selects once whether the run stops after rehearsal."""

    @abstractmethod
    def destination(self, live: RootWriter) -> RootWriter: ...

    @abstractmethod
    def owners_stopped(self, member: type[Install], root: Path, installed: Stack,
                       staged: Stack) -> AbstractContextManager[OwnersStopped]: ...

    @abstractmethod
    def finish[Rehearsed: Rehearsal](self, install: Install[Rehearsed], rehearsal: Rehearsed,
               bridges: SessionBridgeResumes, harnesses: Callable[[Stack], InstallResult],
               report: InstallReport) -> RehearsedUpgrade: ...


@dataclass(frozen=True)
class RehearsalMode(SingleFieldValue, RunMode):
    value: Literal[True] = True

    def destination(self, live: RootWriter) -> RootWriter:
        from agent_comms.installer.reporting import UnpostedWriter
        return UnpostedWriter()

    def owners_stopped(self, member: type[Install], root: Path, installed: Stack,
                       staged: Stack) -> AbstractContextManager[OwnersStopped]:
        return member.owners_kept(root, installed, staged)

    def finish[Rehearsed: Rehearsal](self, install: Install[Rehearsed], rehearsal: Rehearsed,
               bridges: SessionBridgeResumes, harnesses: Callable[[Stack], InstallResult],
               report: InstallReport) -> RehearsedUpgrade:
        return install.reported(rehearsal)


@dataclass(frozen=True)
class LiveMode(SingleFieldValue, RunMode):
    value: Literal[False] = False

    def destination(self, live: RootWriter) -> RootWriter:
        return live

    def owners_stopped(self, member: type[Install], root: Path, installed: Stack,
                       staged: Stack) -> AbstractContextManager[OwnersStopped]:
        return member.owners_stopped(root, installed, staged)

    def finish[Rehearsed: Rehearsal](self, install: Install[Rehearsed], rehearsal: Rehearsed,
               bridges: SessionBridgeResumes, harnesses: Callable[[Stack], InstallResult],
               report: InstallReport) -> RehearsedUpgrade:
        return install.finished(rehearsal, bridges, harnesses, report)
