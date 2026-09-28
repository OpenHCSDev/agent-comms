"""Shared ledger: declaration and persistence owners."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .store_files import _atomic_write_text, _store_lock


class SharedLedger:
    """Persists shared coordination state between threads."""

    def __init__(self, store_path: Path):
        self._path = store_path
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        with _store_lock(self._path):
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        self._data = json.loads(self._path.read_text()) if self._path.exists() else {}

    def _save_unlocked(self) -> None:
        _atomic_write_text(self._path, json.dumps(self._data, indent=2))

    def read(self) -> Mapping[str, object]:
        self._load()
        return dict(self._data)

    def merge(self, updates: Mapping[str, object], author: str) -> None:
        for key in updates:
            if not isinstance(key, str):
                raise ValueError(f"Ledger key must be a string, got {type(key).__name__}.")
        with _store_lock(self._path):
            self._load_unlocked()
            self._data.update(updates)
            self._data["last_updated_by"] = author
            self._save_unlocked()

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

        with _store_lock(self._path):
            self._load_unlocked()
            cleaned, removed = clean(self._data)
            self._data = cleaned if isinstance(cleaned, dict) else {}
            self._save_unlocked()
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

        with _store_lock(self._path):
            self._load_unlocked()
            renamed, changed = rename(self._data)
            self._data = renamed if isinstance(renamed, dict) else {}
            self._save_unlocked()
        return changed
