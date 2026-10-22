"""Shared ledger: declaration and persistence owners."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.cli_result import CliResult
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, JsonShapeFamily, JsonShapeMember
from agent_comms.maintenance_barrier import CarriedDocument
from agent_comms.thread_owned_state import ThreadOwnedState

if TYPE_CHECKING:
    from agent_comms.threads import Thread


class LedgerValue(DeclaredFamily, JsonShapeFamily, affix="LedgerValue"):
    """Free-form JSON decoded once; each shape owns its structural references."""

    def refers_to(self, names: frozenset[str]) -> bool:
        return False

    def without_threads(self, names: frozenset[str]) -> Self:
        return self

    def renamed_thread(self, old: str, new: str) -> LedgerChange[Self]:
        return LedgerChange(self, 0)


@dataclass(frozen=True)
class LedgerChange[Value: LedgerValue]:
    value: Value
    count: int


@dataclass(frozen=True)
class NullLedgerValue(LedgerValue, JsonShapeMember):
    value: None


@dataclass(frozen=True)
class BooleanLedgerValue(LedgerValue, JsonShapeMember):
    value: bool


@dataclass(frozen=True)
class IntegerLedgerValue(LedgerValue, JsonShapeMember):
    value: int


@dataclass(frozen=True)
class NumberLedgerValue(LedgerValue, JsonShapeMember):
    value: float


@dataclass(frozen=True)
class TextLedgerValue(LedgerValue, JsonShapeMember):
    value: str

    def refers_to(self, names: frozenset[str]) -> bool:
        return self.value in names

    def renamed_thread(self, old: str, new: str) -> LedgerChange[Self]:
        return LedgerChange(type(self)(new), 1) if self.value == old else LedgerChange(self, 0)


@dataclass(frozen=True)
class ArrayLedgerValue(LedgerValue, JsonShapeMember):
    value: list[LedgerValue]

    def without_threads(self, names: frozenset[str]) -> Self:
        return type(self)([child.without_threads(names) for child in self.value
                           if not child.refers_to(names)])

    def renamed_thread(self, old: str, new: str) -> LedgerChange[Self]:
        changes = [child.renamed_thread(old, new) for child in self.value]
        return LedgerChange(type(self)([change.value for change in changes]),
                            sum(change.count for change in changes))


@dataclass(frozen=True)
class LedgerDocument(LedgerValue, JsonShapeMember, CliResult):
    """The object shape is also the ledger's document; its keys are its authors'."""

    value: dict[str, LedgerValue]

    def without_threads(self, names: frozenset[str]) -> Self:
        values = {key: child.without_threads(names) for key, child in self.value.items()
                  if key not in names and not child.refers_to(names)}
        return self if values == self.value else type(self)(values)

    def renamed_thread(self, old: str, new: str) -> LedgerChange[Self]:
        values: dict[str, LedgerValue] = {}
        changed = 0
        for key, child in self.value.items():
            change = child.renamed_thread(old, new)
            renamed = new if key == old else key
            values[renamed] = change.value
            changed += change.count + (renamed != key)
        return LedgerChange(type(self)(values), changed)


@dataclass(frozen=True, slots=True)
class SharedLedger(ThreadOwnedState, CarriedDocument[LedgerDocument]):
    """One free-form JSON document; no in-memory mirror."""

    filename: ClassVar[str] = "ledger.json"
    json_indent = 2
    record_type = LedgerDocument

    def empty(self) -> LedgerDocument:
        return LedgerDocument({})

    def merge(self, updates: Mapping[str, object], author: str) -> None:
        changes = FieldCodec.decode(LedgerDocument, dict(updates))
        self.update(lambda document: LedgerDocument(
            {**document.value, **changes.value, "last_updated_by": TextLedgerValue(author)}))

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        names = frozenset(thread.name for thread in threads)
        self.update(lambda document: document.without_threads(names))

    def rename_thread(self, old_name: str, new_name: str) -> int:
        """Replace exact structural references without touching free text."""
        changed = 0

        def change(document: LedgerDocument) -> LedgerDocument:
            nonlocal changed
            renamed = document.renamed_thread(old_name, new_name)
            changed = renamed.count
            return renamed.value if changed else document

        self.update(change)
        return changed
