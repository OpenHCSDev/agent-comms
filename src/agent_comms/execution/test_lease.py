"""One host test controller, owned by its kernel-held file description."""

from __future__ import annotations

import os
import pwd
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, TextIO

from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.storage.store_files import SourceTier
from agent_comms_hooks.process_claim import ClaimHeldError, ProcessSingleton


class LeaseStart(ABC):
    def arguments(self) -> tuple[str, ...]:
        return ()

    def descriptors(self) -> tuple[int, ...]:
        return ()

    @abstractmethod
    def claim(self, owner: ProcessSingleton) -> AbstractContextManager[TextIO]: ...


@dataclass(frozen=True)
class NewTestRun(LeaseStart):
    """A new controller queues in the kernel until the current one lets go: a refusal made
    every caller poll by relaunching pytest, which spent the CPU the lease exists to protect.
    The holder writes who it is into the lease, and a queued run says whom it waits behind:
    on 2026-11-06 a second suite sat silent in pytest_configure for minutes behind a stale run
    of the same session's, and nobody could see it was waiting at all."""

    @contextmanager
    def claim(self, owner: ProcessSingleton) -> Iterator[TextIO]:
        with ExitStack() as lease:
            try:
                held = lease.enter_context(owner.claimed())
            except ClaimHeldError:
                holder = owner.path.read_text(encoding="utf-8").strip() or "an unrecorded run"
                sys.stderr.write(f"Waiting for the host test lease; {holder} holds it.\n")
                sys.stderr.flush()
                held = lease.enter_context(owner.waited())
            held.seek(0)
            held.truncate()
            held.write(f"PID {os.getpid()} since {time.strftime('%H:%M:%S')}"
                       f" ({' '.join(sys.argv)[:160]})\n")
            held.flush()
            yield held


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
    runner_program: ClassVar[str] = ""
    default_start: ClassVar[NewTestRun] = NewTestRun()

    @classmethod
    def in_home(cls, home: Path) -> TestLease:
        return cls(LaunchRuntime.state_directory(home) / "integration-tests.lock")

    @classmethod
    def for_runner_programs(cls, programs: tuple[Path, ...], home: Path) -> TestLease:
        """Decode the runner's process lineage, not CI/HOME environment flags."""
        names = frozenset(program.name for program in programs)
        member = next(
            (member for member in cls.members_with(cls) if member.runner_program in names),
            HostTestLease,
        )
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
    def held(self, start: LeaseStart = default_start) -> Iterator[LeaseStart]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with ExitStack() as lifetime:
            held = lifetime.enter_context(start.claim(self))
            yield InheritedTestRun(held.fileno())


class HostTestLease(TestLease):
    """All local worktrees and changed HOME values share the UID's real home."""


class GithubActionsTestLease(TestLease):
    """The declared GitHub worker owns the CI job, not an agent-set CI flag."""

    runner_program = "Runner.Worker"

    @contextmanager
    def held(self, start: LeaseStart = TestLease.default_start) -> Iterator[LeaseStart]:
        yield self.default_start
