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
    a frame reach subscribers as the difference from the last published rows.
    """

    def __init__(self, schedule: Callable[[Callable[[], None]], None]):
        self._schedule = schedule
        self.rows: dict[Key, Row] = {}
        self._subscribers: list[Callable[[Changes[Key]], None]] = []
        self._published: dict[Key, Row] = self.rows
        self._scheduled = False

    def subscribe(self, callback: Callable[[Changes[Key]], None]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[Changes[Key]], None]) -> None:
        self._subscribers.remove(callback)

    def replace(self, rows: Mapping[Key, Row]) -> None:
        """Retain the newest rows; compare only the frame's final answer."""
        self.rows = dict(rows)
        if not self._scheduled:
            self._scheduled = True
            self._schedule(self.flush)

    def flush(self) -> None:
        """Publish only differences from the rows subscribers last received."""
        self._scheduled = False
        previous, current = self._published, self.rows
        order = tuple(current)
        changes = Changes(
            added=tuple(key for key in current if key not in previous),
            removed=tuple(key for key in previous if key not in current),
            changed=tuple(key for key, row in current.items() if key in previous and previous[key] != row),
            order=order if order != tuple(previous) else None,
        )
        self._published = current
        if changes:
            for callback in tuple(self._subscribers):
                callback(changes)
