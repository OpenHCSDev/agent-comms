"""Observe real native command returns and their actual output or stored publication."""

from __future__ import annotations

import argparse
import io
import json
import sys
from abc import abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import CodeType, MethodType
from typing import TypeGuard

import agent_comms.standing_rules as native_rules
from agent_comms.commander import capacity
from agent_comms.commander.cli import main as commander_main
from agent_comms.commander.cli import tool_name
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.order import Commander
from agent_comms.commander.send_guard import CommanderSession, RawSendCommand
from agent_comms.commander_heartbeat import CommanderReview
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.native_tools import NativeToolDeclaration
from agent_comms.turn_context import InstructionSegment


@dataclass(frozen=True)
class MeasuredOutput:
    current: str
    native: str
    source: str
    root: str
    commander: str


class NativeObservation(DeclaredFamily, affix="Observation"):
    @classmethod
    def arguments(cls) -> tuple[str, ...]:
        return (str(Path(__file__).resolve()), cls.declared_name)

    @abstractmethod
    def read(self, root: Path) -> MeasuredOutput: ...


def method_code(method: object) -> CodeType:
    if not isinstance(method, MethodType):
        raise TypeError("An observation requires a bound Python method")
    return method.__func__.__code__


@contextmanager
def returned_values(callbacks: dict[CodeType, Callable[[object], None]]) -> Iterator[None]:
    """Observe only declared native returns, not every Python call in the command."""
    monitor = sys.monitoring
    tool = monitor.PROFILER_ID

    def returned(code: CodeType, offset: int, value: object) -> None:
        callbacks[code](value)

    monitor.use_tool_id(tool, "commander-parity-observation")
    try:
        monitor.register_callback(tool, monitor.events.PY_RETURN, returned)
        for code in callbacks:
            monitor.set_local_events(tool, code, monitor.events.PY_RETURN)
        yield
    finally:
        for code in callbacks:
            monitor.set_local_events(tool, code, 0)
        monitor.register_callback(tool, monitor.events.PY_RETURN, None)
        monitor.free_tool_id(tool)


class CapacityObservation(NativeObservation):
    def read(self, root: Path) -> MeasuredOutput:
        measured: list[capacity.Load] = []
        contexts: list[Commander] = []

        def measurement(value: object) -> None:
            if not isinstance(value, capacity.Load):
                raise TypeError("Load.of returned outside its declared contract")
            measured.append(value)

        def context(value: object) -> None:
            if not isinstance(value, Commander):
                raise TypeError("The commander CLI returned an undeclared context")
            contexts.append(value)

        output = io.StringIO()
        # This process owns its command line. The actual CLI selects its root and command.
        sys.argv[1:] = ["--root", str(root), tool_name(capacity.__name__)]
        with (
            returned_values(
                {
                    method_code(capacity.Load.of): measurement,
                    method_code(Commander.read): context,
                }
            ),
            redirect_stdout(output),
        ):
            commander_main()
        if len(measured) != 1 or len(contexts) != 1:
            raise ValueError("Capacity must use one native measurement and selected context")
        selected = contexts[0]
        return MeasuredOutput(
            output.getvalue(),
            str(measured[0]),
            str(capacity.__file__),
            str(selected.comms.root),
            selected.commander,
        )


def standing_rule_return(value: object) -> TypeGuard[tuple[native_rules.StandingRule, ...]]:
    """Validate CPython's untyped return, without choosing rule variants."""
    return isinstance(value, tuple) and all(
        isinstance(rule, native_rules.StandingRule) for rule in value
    )


class StandingRulesObservation(NativeObservation):
    def read(self, root: Path) -> MeasuredOutput:
        if root.resolve() == CommanderConfig.read(CommanderConfig.path()).root.resolve():
            raise ValueError("Heartbeat publication verification requires an isolated bus copy")
        config = CommanderConfig.read_root(root)
        comms = Comms(config.root)
        measured: list[tuple[str, ...]] = []
        before = comms.bus.log.latest_sequence()

        def returned(value: object) -> None:
            if not standing_rule_return(value):
                raise TypeError("StandingRule.broken returned outside its declared contract")
            measured.append(tuple(rule.line() for rule in value))

        with returned_values({method_code(native_rules.StandingRule.broken): returned}):
            CommanderReview().wake(
                comms, config.commander, comms.registry.require(config.commander)
            )
        with comms.bus.log.opened_wire_snapshot() as source:
            published = tuple(
                message for message in source.newest_messages() if message.seq > before
            )
        if len(measured) != 1 or len(published) != int(bool(measured[0])):
            raise ValueError("Heartbeat must consume one observation and commit its actual message")
        if published and (
            published[0].sender != config.commander or published[0].target != config.commander
        ):
            raise ValueError("Heartbeat published outside its configured commander route")
        lines = tuple(
            line.removeprefix("RULE ")
            for message in published
            for line in message.body.splitlines()
            if line.startswith("RULE ")
        )
        return MeasuredOutput(
            "\n".join(lines),
            "\n".join(measured[0]),
            str(native_rules.__file__),
            str(comms.root),
            config.commander,
        )


class RuleReviewObservation(NativeObservation):
    def read(self, root: Path) -> MeasuredOutput:
        config = CommanderConfig.read_root(root)
        current = tuple(
            rule.line()
            for rule in native_rules.RulesDue.violations(Comms(config.root), config.commander)
        )
        segments = tuple(
            segment.__name__ for segment in InstructionSegment.members_with(InstructionSegment)
        )
        raise SystemExit(
            "Missing native owner: commander rule-review InstructionSegment with rule-file "
            f"provenance. Current RulesDue={current!r}; "
            f"native instruction declarations={segments!r}"
        )


class OrderContactObservation(NativeObservation):
    def read(self, root: Path) -> MeasuredOutput:
        config = CommanderConfig.read_root(root)
        context = Commander.read_root(config.root)
        units = sorted(context.units())
        current = tuple(
            RawSendCommand.refused(
                ("agent-comms", "send", "--from", config.commander, "--to", unit),
                CommanderSession("", context),
            )
            for unit in units[:1]
        )
        native = tuple(
            tool.declared_name for tool in NativeToolDeclaration.members_with(NativeToolDeclaration)
        )
        raise SystemExit(
            "Missing native owner: order-only-contact native harness hook "
            "checking Core order tools. "
            f"Current RawSendCommand={current!r}; native coding declarations={native!r}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observation", type=NativeObservation.member_named)
    parser.add_argument(
        "--root", type=Path, default=CommanderConfig.read(CommanderConfig.path()).root
    )
    arguments = parser.parse_args()
    observed = arguments.observation().read(arguments.root)
    print(json.dumps(FieldCodec.encode(observed)))


if __name__ == "__main__":
    main()
