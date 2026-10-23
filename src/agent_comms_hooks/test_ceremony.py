"""Local test runs answer only what the change touched.

The full suite runs in CI for every commit on main; a local run of the whole tree spends the
machine for an answer that already exists, and a fleet of agents doing it at once starves every
other run on the box. A local run names the test files the change exercises.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Self

from agent_comms_hooks import BashRefusal
from agent_comms_hooks.shell import Program, ShellCommand

PYTEST = Program("pytest")


@dataclass(frozen=True)
class PytestRun:
    """One pytest invocation's positional arguments: the tests it selects."""

    selected: tuple[str, ...]

    # pytest's own options that take their value as the next word (its external contract)
    valued: ClassVar[frozenset[str]] = frozenset(
        {
            "-n",
            "-p",
            "-o",
            "-k",
            "-m",
            "-c",
            "-W",
            "--basetemp",
            "--rootdir",
            "--durations",
            "--deselect",
            "--ignore",
            "--tb",
            "--confcutdir",
            "--maxfail",
        }
    )

    @classmethod
    def of(cls, command: ShellCommand) -> tuple[Self, ...]:
        args = command.args
        if PYTEST.runs(command):
            return (cls.selecting(args),)
        if command.program.startswith("python") and "-m" in args[:-1]:
            module = args.index("-m")
            if args[module + 1] == PYTEST.name:
                return (cls.selecting(args[module + 2 :]),)
        return ()

    @classmethod
    def selecting(cls, args: list[str]) -> Self:
        selected, words = [], iter(args)
        for word in words:
            if "<" in word or ">" in word:
                break  # a redirection: the words from here are the shell's, not pytest's
            if word in cls.valued:
                next(words, None)
            elif not word.startswith("-"):
                selected.append(word)
        return cls(tuple(selected))

    def names_a_test(self) -> bool:
        """Whether it selects a test module or test id, not only directories."""
        return any(word.endswith(".py") or "::" in word for word in self.selected)


class TestCeremony(ABC):
    """One way a local test run spends the box on an answer CI or a narrower run gives."""

    reason: ClassVar[str]

    @classmethod
    @abstractmethod
    def wastes(cls, run: PytestRun) -> bool: ...

    @classmethod
    def found(cls, run: PytestRun) -> tuple[str, ...]:
        return tuple(member.reason for member in cls.__subclasses__() if member.wastes(run))


class WholeSuite(TestCeremony):
    reason = (
        "a run of the whole test tree (no test module named, only directories or"
        " nothing): the full suite runs in CI"
    )

    @classmethod
    def wastes(cls, run: PytestRun) -> bool:
        return not run.names_a_test()


class TestCeremonyGuard(BashRefusal):
    headline = "Test ceremony refused"
    advice = (
        ". Run the test files this change exercises; failures already on main are CI's"
        " answer for the base commit, not a reason to rerun main."
    )

    @classmethod
    def reasons(cls, command: str) -> tuple[str, ...]:
        return tuple(
            reason
            for simple in ShellCommand.split(command)
            for run in PytestRun.of(simple)
            for reason in TestCeremony.found(run)
        )


main = TestCeremonyGuard.main


if __name__ == "__main__":
    main()
