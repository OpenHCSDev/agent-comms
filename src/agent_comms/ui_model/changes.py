"""Keyed presentation models that report what changed, flushed once per frame."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

Key = TypeVar("Key", bound=Hashable)
Row = TypeVar("Row")


@dataclass(frozen=True)
class Changes(Generic[Key]):
    """What changed since the last flush; a view updates exactly these rows."""

    added: tuple[Key, ...] = ()
    removed: tuple[Key, ...] = ()
    changed: tuple[Key, ...] = ()
    order: tuple[Key, ...] | None = None

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed or self.order is not None)


class KeyedModel(Generic[Key, Row]):
    """Ordered rows keyed by identity; replacements become one change set per flush.

    ``schedule`` is the backend's "run this once before the next frame"; the
    model calls it at most once between flushes, so any number of updates in
    a frame reach subscribers as one merged change set.
    """

    def __init__(self, schedule: Callable[[Callable[[], None]], None]):
        self._schedule = schedule
        self.rows: dict[Key, Row] = {}
        self._subscribers: list[Callable[[Changes[Key]], None]] = []
        self._added: dict[Key, None] = {}
        self._removed: dict[Key, None] = {}
        self._changed: dict[Key, None] = {}
        self._order: tuple[Key, ...] | None = None
        self._scheduled = False

    def subscribe(self, callback: Callable[[Changes[Key]], None]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[Changes[Key]], None]) -> None:
        self._subscribers.remove(callback)

    def replace(self, rows: Mapping[Key, Row]) -> None:
        """Make ``rows`` (in their order) the model's rows, recording the difference."""
        previous = self.rows
        for key in previous.keys() - rows.keys():
            if self._added.pop(key, ...) is ...:
                self._removed[key] = None
            self._changed.pop(key, None)
        for key, row in rows.items():
            before = previous.get(key)
            if before is None:
                if self._removed.pop(key, ...) is ...:
                    self._added[key] = None
                else:
                    # Removed and re-added in one frame: the view keeps its row.
                    self._changed[key] = None
            elif key not in self._added and before != row:
                self._changed[key] = None
        if tuple(previous) != tuple(rows):
            self._order = tuple(rows)
        self.rows = dict(rows)
        if (self._added or self._removed or self._changed or self._order is not None) and not self._scheduled:
            self._scheduled = True
            self._schedule(self.flush)

    def flush(self) -> None:
        """Deliver the merged changes since the last flush to every subscriber."""
        self._scheduled = False
        changes = Changes(tuple(self._added), tuple(self._removed), tuple(self._changed), self._order)
        self._added, self._removed, self._changed, self._order = {}, {}, {}, None
        if changes:
            for callback in tuple(self._subscribers):
                callback(changes)
