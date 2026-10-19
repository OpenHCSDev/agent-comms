"""Shared ledger: declaration and persistence owners."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, cast

from .cli_result import CliResult
from .field_codec import FieldCodec, SingleFieldValue
from .locked_store import LockedStore, RootFile
from .thread_owned_state import ThreadOwnedState

if TYPE_CHECKING:
    from .threads import Thread


@dataclass(frozen=True)
class LedgerDocument(SingleFieldValue, CliResult):
    """The ledger's one free-form JSON document as read; its keys are its authors'."""

    entries: dict[str, object]

@dataclass(frozen=True, slots=True)
class SharedLedger(RootFile, ThreadOwnedState, LockedStore[LedgerDocument]):
    """One free-form JSON document; no in-memory mirror or closed value family."""

    filename: ClassVar[str] = "ledger.json"
    json_indent = 2

    record_type = LedgerDocument

    def empty(self) -> LedgerDocument:
        return LedgerDocument({})

    def merge(self, updates: Mapping[str, object], author: str) -> None:
        changes = FieldCodec.decode(LedgerDocument, dict(updates))
        self.update(lambda document: LedgerDocument(
            {**document.entries, **changes.entries, "last_updated_by": author}))

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """Remove exact structural references to these thread names."""
        names = frozenset(thread.name for thread in threads)

        def named(value: object) -> bool:
            return isinstance(value, str) and value in names

        def clean(value: object) -> tuple[object, int]:
            if isinstance(value, dict):
                result: dict[str, object] = {}
                removed = 0
                for key, child in value.items():
                    if key in names or named(child):
                        removed += 1
                        continue
                    cleaned, count = clean(child)
                    result[key] = cleaned
                    removed += count
                return result, removed
            if isinstance(value, list):
                result_list: list[object] = []
                removed = 0
                for child in value:
                    if named(child):
                        removed += 1
                        continue
                    cleaned, count = clean(child)
                    result_list.append(cleaned)
                    removed += count
                return result_list, removed
            return value, 0

        removed = 0

        def change(document: LedgerDocument) -> LedgerDocument:
            nonlocal removed
            cleaned, removed = clean(document.entries)
            return LedgerDocument(cast(dict[str, object], cleaned)) if removed else document

        self.update(change)

    def rename_thread(self, old_name: str, new_name: str) -> int:
        """Replace exact structural references without touching free text."""

        def rename(value: object) -> tuple[object, int]:
            if isinstance(value, dict):
                result: dict[str, object] = {}
                changed = 0
                for key, child in value.items():
                    renamed_child, count = rename(child)
                    renamed_key = new_name if key == old_name else key
                    result[renamed_key] = renamed_child
                    changed += count + (renamed_key != key)
                return result, changed
            if isinstance(value, list):
                result_list: list[object] = []
                changed = 0
                for child in value:
                    renamed_child, count = rename(child)
                    result_list.append(renamed_child)
                    changed += count
                return result_list, changed
            if value == old_name:
                return new_name, 1
            return value, 0

        changed = 0

        def change(document: LedgerDocument) -> LedgerDocument:
            nonlocal changed
            renamed, changed = rename(document.entries)
            return LedgerDocument(cast(dict[str, object], renamed)) if changed else document

        self.update(change)
        return changed
