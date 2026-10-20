"""Compare current commander wording checks with their real native text owners."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable
from contextlib import redirect_stderr
from dataclasses import dataclass
from typing import ClassVar

from ..commander.config import CommanderConfig
from ..commander.order import refuse
from ..comms import Comms
from ..errors import ToolRefusedError
from ..todos import OrderText, OrderWords, PlainText, Todo
from . import ParityComparison, UnverifiedParity


@dataclass(frozen=True)
class WordingOutcome:
    accepted: bool
    notes: str

    @classmethod
    def read(cls, operation: Callable[[], object]) -> WordingOutcome:
        notes = io.StringIO()
        try:
            with redirect_stderr(notes):
                operation()
        except (SystemExit, ToolRefusedError):
            return cls(False, notes.getvalue())
        return cls(True, notes.getvalue())


def history() -> tuple[Todo, ...]:
    config = CommanderConfig.read(CommanderConfig.path())
    comms = Comms(config.root)
    actor = comms.registry.require(config.commander).incarnation
    return comms.goals.todos.history_for_commander(actor)


@dataclass(frozen=True)
class WordingComparison(ParityComparison):
    observations: tuple[tuple[WordingOutcome, WordingOutcome], ...]
    owner: str
    native_owner: ClassVar[type[OrderWords]]

    @property
    def passed(self) -> bool:
        return bool(self.observations) and all(a == b for a, b in self.observations)

    def describe(self) -> str:
        return (f"{len(self.observations)} retained texts through commander.order.refuse and "
                f"{self.owner}; compared refusal and actual wording warnings; no publication")

    @classmethod
    def compare_texts(cls, texts: Iterable[str]) -> ParityComparison:
        observed = tuple(
            (WordingOutcome.read(lambda: refuse(text, cls.native_owner)),
             WordingOutcome.read(lambda: cls.native_owner(text)))
            for text in texts
        )
        if not observed:
            return UnverifiedParity("No retained text exists for this wording comparison.")
        return cls(observed, f"{cls.native_owner.__module__}.{cls.native_owner.__qualname__}")


class PlainWordsComparison(WordingComparison):
    native_owner = PlainText

    @classmethod
    def read(cls) -> ParityComparison:
        return cls.compare_texts(strike.reason for todo in history() for strike in todo.strikes)


class OrderWordingComparison(WordingComparison):
    native_owner = OrderText

    @classmethod
    def read(cls) -> ParityComparison:
        return cls.compare_texts(
            text for todo in history() if todo.pending
            for text in (*todo.order.checked().edits, todo.order.checked().report)
        )
