"""Routing annotations keyed by durable Pi entry IDs, never inferred from reply text."""

import json
from collections.abc import Mapping
from pathlib import Path

from .declarations import TurnRouting, _atomic_write_text, _store_lock, file_revision


class TranscriptRoutes:
    def __init__(self, path: Path):
        self.path = path
        self._revision: tuple | None = None
        self._entries: dict[str, dict[str, TurnRouting]] = {}

    def _load(self) -> None:
        revision = file_revision(self.path)
        if revision != self._revision:
            raw = json.loads(self.path.read_text()) if revision else {}
            self._entries = {
                path: {entry: TurnRouting.from_wire(route) for entry, route in entries.items()}
                for path, entries in raw.items()
            }
            self._revision = revision

    def for_session(self, session_file: str) -> Mapping[str, TurnRouting]:
        with _store_lock(self.path):
            self._load()
            return dict(self._entries.get(session_file, {}))

    def record(self, session_file: str, entry_ids: tuple[str, ...], routing: TurnRouting) -> None:
        if not entry_ids:
            return
        with _store_lock(self.path):
            self._load()
            entries = self._entries.setdefault(session_file, {})
            entries.update(dict.fromkeys(entry_ids, routing))
            self._revision = None
            _atomic_write_text(
                self.path,
                json.dumps(
                    {
                        path: {entry: route.to_wire() for entry, route in items.items()}
                        for path, items in self._entries.items()
                    },
                    indent=2,
                ),
            )
