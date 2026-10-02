"""Shared ledger: declaration and persistence owners."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, cast

from .field_codec import FieldCodec
from .locked_store import LockedStore


@dataclass(frozen=True, slots=True)
class SharedLedger(LockedStore[dict[str, Any]]):
    """One free-form JSON document; no in-memory mirror or closed value family."""

    filename: ClassVar[str] = "ledger.json"
    json_indent = 2

    @property
    def record_type(self) -> type[dict[str, Any]]:
        return dict[str, Any]

    def empty(self) -> dict[str, Any]:
        return {}

    def merge(self, updates: Mapping[str, object], author: str) -> None:
        changes = FieldCodec.decode(self.record_type, dict(updates))
        self.update(lambda values: {**values, **changes, "last_updated_by": author})

    def remove_thread(self, name: str) -> int:
        """Remove exact structural references to a thread identity."""

        def clean(value: object) -> tuple[object, int]:
            if isinstance(value, dict):
                result: dict[str, object] = {}
                removed = 0
                for key, child in value.items():
                    if key == name or child == name:
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
                    if child == name:
                        removed += 1
                        continue
                    cleaned, count = clean(child)
                    result_list.append(cleaned)
                    removed += count
                return result_list, removed
            return value, 0

        removed = 0

        def change(values: dict[str, Any]) -> dict[str, Any]:
            nonlocal removed
            cleaned, removed = clean(values)
            return cast(dict[str, Any], cleaned) if removed else values

        self.update(change)
        return removed

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

        def change(values: dict[str, Any]) -> dict[str, Any]:
            nonlocal changed
            renamed, changed = rename(values)
            return cast(dict[str, Any], renamed) if changed else values

        self.update(change)
        return changed
