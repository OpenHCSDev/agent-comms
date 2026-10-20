"""Observe real native command returns and their actual output."""

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

from agent_comms.commander import capacity
from agent_comms.commander.cli import main as commander_main
from agent_comms.commander.cli import tool_name
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.order import Commander
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec


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
                    method_code(Commander.read_root): context,
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
