"""One host test controller, owned by its kernel-held file description."""

from __future__ import annotations

import os
import pwd
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, TextIO

from .child_process import ProcessIdentity
from .launch_runtime import LaunchRuntime
from .process_claim import ClaimHeldError, ProcessSingleton
from .store_files import SourceTier


class TestLeaseBusy(RuntimeError):
    """Another controller is already spending this host's test budget."""


class LeaseStart(ABC):
    def arguments(self) -> tuple[str, ...]:
        return ()

    def descriptors(self) -> tuple[int, ...]:
        return ()

    @abstractmethod
    def claim(self, owner: ProcessSingleton) -> AbstractContextManager[TextIO]: ...


@dataclass(frozen=True)
class NewTestRun(LeaseStart):
    def claim(self, owner: ProcessSingleton) -> AbstractContextManager[TextIO]:
        return owner.claimed()


@dataclass(frozen=True)
class InheritedTestRun(LeaseStart):
    descriptor: int
    option: ClassVar[str] = "--integration-lease"

    def arguments(self) -> tuple[str, ...]:
        return self.option, str(self.descriptor)

    def descriptors(self) -> tuple[int, ...]:
        return (self.descriptor,)

    @classmethod
    def argument(cls, value: str) -> InheritedTestRun:
        return cls(int(value))

    def claim(self, owner: ProcessSingleton) -> AbstractContextManager[TextIO]:
        # Adoption verifies the descriptor's inode and exclusive claim. A number
        # supplied by a caller is not permission to skip the host lease.
        return owner.adopted(self.descriptor)


class TestLease(ProcessSingleton, SourceTier, declared_name=None):
    refusal: ClassVar[str] = "integration run already holds the test lease"
    runner_program: ClassVar[str] = ""

    @classmethod
    def in_home(cls, home: Path) -> TestLease:
        return cls(LaunchRuntime.state_directory(home) / "integration-tests.lock")

    @classmethod
    def for_runner_programs(cls, programs: tuple[Path, ...], home: Path) -> TestLease:
        """Decode the runner's process lineage, not CI/HOME environment flags."""
        names = frozenset(program.name for program in programs)
        member = next((member for member in cls.members_with(cls)
                       if member.runner_program in names), HostTestLease)
        return member.in_home(home)

    @classmethod
    def current(cls) -> TestLease:
        programs = []
        for process in ProcessIdentity.capture(os.getpid()).lineage():
            process.require_alive()
            programs.append(Path(process.arguments()[0]))
            process.require_alive()
        account = pwd.getpwuid(os.getuid())
        return cls.for_runner_programs(tuple(programs), Path(account.pw_dir))

    @contextmanager
    def held(self, start: LeaseStart = NewTestRun()) -> Iterator[LeaseStart]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with ExitStack() as lifetime:
            try:
                held = lifetime.enter_context(start.claim(self))
            except ClaimHeldError as error:
                raise TestLeaseBusy(self.refusal) from error
            yield InheritedTestRun(held.fileno())


class HostTestLease(TestLease):
    """All local worktrees and changed HOME values share the UID's real home."""


class GithubActionsTestLease(TestLease):
    """The declared GitHub worker owns the CI job, not an agent-set CI flag."""

    runner_program = "Runner.Worker"

    @contextmanager
    def held(self, start: LeaseStart = NewTestRun()) -> Iterator[LeaseStart]:
        yield NewTestRun()
