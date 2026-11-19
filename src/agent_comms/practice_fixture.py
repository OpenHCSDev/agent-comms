"""Prepare and advance isolated real agent-comms roots for CPU practice tasks.

Learner actions use the ordinary ``agent-comms --root ROOT`` CLI. This entry
point only owns fixture setup and harness-triggered one-shot transitions.
"""
from __future__ import annotations

import argparse
import sys
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms, client_wire, wire
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.send_result import MessageSentResult
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.registry.threads import ExternalThread


@dataclass(frozen=True)
class FixtureRequest:
    root: Path
    worktree: Path
    reader: str
    sender: str
    target: str
    replacement: str
    body: str


class PracticeScenario(DeclaredFamily, affix="Scenario", declared_name=None):
    """One real-bus starting state. The registry and bus remain the identity stores."""

    @abstractmethod
    def prepare(self, comms: Comms, request: FixtureRequest) -> None: ...

    @staticmethod
    def declare(comms: Comms, request: FixtureRequest, *names: str) -> None:
        for name in dict.fromkeys(names):
            directory = request.worktree / name
            directory.mkdir(parents=True, exist_ok=True)
            comms.registry.declare(ExternalThread(name, frozenset({"practice"}),
                                                  Checkout(directory)))


class EmptyInboxScenario(PracticeScenario):
    def prepare(self, comms: Comms, request: FixtureRequest) -> None:
        self.declare(comms, request, request.reader, request.sender)


class LateMessageScenario(PracticeScenario):
    """Start empty; the CPU harness invokes PublishFixtureMessage after observing that read."""

    def prepare(self, comms: Comms, request: FixtureRequest) -> None:
        self.declare(comms, request, request.reader, request.sender)


class ArchivedTargetScenario(PracticeScenario):
    def prepare(self, comms: Comms, request: FixtureRequest) -> None:
        self.declare(comms, request, request.reader, request.sender, request.target)
        comms.threads.archive(request.target)
        self.declare(comms, request, request.replacement)


class ReplacedTargetScenario(PracticeScenario):
    """Archive, delete and redeclare the same name to produce a new incarnation."""

    def prepare(self, comms: Comms, request: FixtureRequest) -> None:
        self.declare(comms, request, request.reader, request.sender, request.target)
        before = comms.registry.all_threads()[request.target].incarnation
        comms.threads.archive(request.target)
        comms.threads.delete(request.target)
        self.declare(comms, request, request.target)
        after = comms.registry.all_threads()[request.target].incarnation
        if after == before:
            raise RuntimeError("fixture replacement retained the deleted thread incarnation")


@dataclass(frozen=True)
class PreparedFixture(CliResult):
    scenario: type[PracticeScenario]


def prepare(request: FixtureRequest, scenario: type[PracticeScenario]) -> PreparedFixture:
    request.root.mkdir(parents=True, exist_ok=False)
    Bus.install(Bus.path_in(request.root))
    StorageRule(floor_gib=0, cleanup_gib=0, memory_gib=0).prepare_isolated(request.root)
    comms = wire(request.root)
    scenario().prepare(comms, request)
    return PreparedFixture(scenario)


class FixtureOperation(DeclaredFamily, affix="FixtureOperation", declared_name=None):
    """One entry-point operation. Each member owns its arguments and result."""

    @classmethod
    @abstractmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None: ...

    @classmethod
    @abstractmethod
    def execute(cls, args: argparse.Namespace) -> CliResult: ...


class PrepareFixtureOperation(FixtureOperation):
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        parser = subparsers.add_parser(cls.declared_name)
        parser.add_argument("--root", required=True, type=Path)
        parser.add_argument("--worktree", required=True, type=Path)
        parser.add_argument("--scenario", required=True, choices=PracticeScenario.names())
        parser.add_argument("--reader", required=True)
        parser.add_argument("--sender", required=True)
        parser.add_argument("--target", default="target")
        parser.add_argument("--replacement", default="replacement")
        parser.add_argument("--body", default="fixture message")
        parser.set_defaults(operation=cls)

    @classmethod
    def execute(cls, args: argparse.Namespace) -> CliResult:
        request = FixtureRequest(args.root, args.worktree, args.reader, args.sender,
                                 args.target, args.replacement, args.body)
        return prepare(request, PracticeScenario.member_named(args.scenario))


class PublishFixtureMessageOperation(FixtureOperation):
    """One synchronous real publication after the harness observed the empty read; never retried."""

    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        parser = subparsers.add_parser(cls.declared_name)
        parser.add_argument("--root", required=True, type=Path)
        parser.add_argument("--from", required=True, dest="sender")
        parser.add_argument("--to", required=True, dest="target")
        parser.add_argument("--body", required=True)
        parser.set_defaults(operation=cls)

    @classmethod
    def execute(cls, args: argparse.Namespace) -> CliResult:
        with client_wire(args.root) as comms:
            return MessageSentResult(comms.messaging.send(args.sender, args.target, args.body))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="agent-comms-practice-fixture")
    subparsers = result.add_subparsers(required=True)
    for operation in FixtureOperation.members_with(FixtureOperation):
        operation.add_parser(subparsers)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.operation.execute(args).write(sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
