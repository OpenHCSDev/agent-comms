"""The existing registry constructor and turn transactions, below turn values."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from pydantic import JsonValue

from agent_comms.registry.thread_identity import TurnIdentity


class TurnRows(ABC):
    """Where a registry keeps its leased turns' records, in their wire form."""

    @abstractmethod
    def turn_wires(self, identities: tuple[TurnIdentity, ...]) -> Mapping[TurnIdentity, JsonValue]:
        """Each named turn's record from one committed read; a retired turn has none."""

    @abstractmethod
    def change_turn_wire[Result](
        self, identity: TurnIdentity,
        change: Callable[[JsonValue], Iterable[tuple[JsonValue, Result]]],
    ) -> tuple[Result, ...]:
        """Apply ``change`` to one turn's record in one transaction of its own; the last
        record it yields is written, and no registry reader is woken by it."""


class TurnRegistry[Record, Declaration](ABC):
    path: Path
    rows: TurnRows

    @abstractmethod
    def __init__(self, path: Path) -> None: ...

    def read_turn(self, held: Record) -> Declaration:
        turn, = self.read_turns((held,))
        return turn

    @abstractmethod
    def read_turns(self, held: tuple[Record, ...]) -> tuple[Declaration, ...]:
        """Each held turn, in order, under one registry copy."""

