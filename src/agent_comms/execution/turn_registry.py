"""The existing registry constructor and turn transactions, below turn values."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path


class TurnRegistry[Record, Declaration, Locked](ABC):
    path: Path

    @abstractmethod
    def __init__(self, path: Path) -> None: ...

    def read_turn(self, held: Record) -> Declaration:
        turn, = self.read_turns((held,))
        return turn

    @abstractmethod
    def read_turns(self, held: tuple[Record, ...]) -> tuple[Declaration, ...]:
        """Each held turn, in order, under one registry copy."""

    @abstractmethod
    def change_turn[Result](
        self, record: Record, change: Callable[[Locked], Result],
    ) -> Result: ...
