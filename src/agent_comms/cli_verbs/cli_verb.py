"""Lightweight base contract for agent-comms CLI verbs.

Target-menu binding and interactive editor machinery live on ``TargetCommand``
in ``agent_comms.cli_verbs.cli_command`` so plain CLI verbs and ``cli_runtime``
do not import registry/config/thread models at startup.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from inspect import iscoroutinefunction, signature
from types import get_original_bases
from typing import Generic, TextIO, TypeVar, cast, get_args, get_origin

from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.cli_verbs.declared_verb import DeclaredCommand
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.installer.active_route import ActiveRoute, CommsRoute
from agent_comms.nominal.declared_family import ModuleMembers
from agent_comms.planning.command import Command


class CliBus(ABC):
    """The bus context CLI commands run against; Comms supplies the live composition root."""


type BusOpener = Callable[[CommsRoute], AbstractContextManager[CliBus]]


class CliExecution[Answer: CliResult = CliResult](Command[CliBus, Answer]):
    """The dispatcher contract, including methods selected through multiple inheritance."""

    @abstractmethod
    def execute(self, route: CommsRoute, output: TextIO, open_bus: BusOpener) -> int:
        """Execute against the selected route and write to the caller's stream."""

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        expected, actual = (
            (iscoroutinefunction(method),
             tuple((parameter.kind, parameter.default)
                   for parameter in signature(method, follow_wrapped=False).parameters.values()))
            for method in (CliExecution.execute, cls.execute)
        )
        if actual != expected:
            raise TypeError(f"{cls.__qualname__}.execute{signature(cls.execute)} disagrees with "
                            f"CliExecution.execute{signature(CliExecution.execute)}")


Answer = TypeVar("Answer", bound=CliResult, covariant=True, default=CliResult)


@dataclass(frozen=True, kw_only=True)
class CliCommand(
    ModuleMembers,
    DeclaredCommand,
    CliExecution[Answer],
    Generic[Answer],
    affix="CliCommand",
    discovery_package="agent_comms.cli_verbs",
):
    @classmethod
    def result_type(cls) -> type[Answer]:
        """The result this command declares once, as the family's type argument in its own or
        an inherited class header."""
        commands = cls.__mro__[:cls.__mro__.index(CliCommand) + 1]
        for declaration in commands:
            for base in get_original_bases(declaration):
                if get_origin(base) in commands:
                    (answer,) = get_args(base)
                    return cast(type[Answer], answer)
        raise TypeError(f"{cls.__qualname__} declares no result in its class header")

    @classmethod
    def invocation(cls, *arguments: str) -> tuple[str, ...]:
        """This command's invocation on the agent-comms command line."""
        return (cls.declared_name, *arguments)

    def run(self, ctx: CliBus, output: TextIO) -> int:
        """Run this command and write its declared result."""
        return self.apply(ctx).write(output)

    @classmethod
    def published_launch(cls) -> LaunchRoot:
        """The root a command reads when no --root is given."""
        return ActiveRoute.published_launch()

    def execute(self, route: CommsRoute, output: TextIO, open_bus: BusOpener) -> int:
        """Open the bus this command reads, then run it; a command that must act before
        the bus opens overrides this."""
        with open_bus(route) as comms:
            return self.run(comms, output)
