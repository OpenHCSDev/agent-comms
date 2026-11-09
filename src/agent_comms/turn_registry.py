"""The existing registry constructor and turn transactions, below turn values."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path


class TurnRegistry[Record, Declaration, Locked](ABC):
    path: Path

    @abstractmethod
    def __init__(self, path: Path) -> None: ...

    @abstractmethod
    def read_turn(self, held: Record) -> Declaration: ...

    @abstractmethod
    def change_turn[Result](
        self, record: Record, change: Callable[[Locked], Result],
    ) -> Result: ...
