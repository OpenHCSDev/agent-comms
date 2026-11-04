"""How a thread shows one of the history sources its runtime offers: its own, or a parent's
seen from a fork."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import TypeVar


class Dated(ABC):
    """A record with the time it happened, comparable against a moment."""

    timestamp: float

    def before(self, at: float) -> bool:
        return self.timestamp < at


Event = TypeVar("Event", bound=Dated)


class HistoryView(ABC):
    """A thread's own history shows every record and no fork rows."""

    def kept(self, events: tuple[Event, ...]) -> tuple[Event, ...]:
        return events

    def framed(
        self, events: tuple[Event, ...], fork_rows: tuple[Event, ...], *,
        ends_at_fork: bool, reaches_start: bool,
    ) -> tuple[Event, ...]:
        """One page's events with the fork rows where this view places them."""
        return events


@dataclass(frozen=True, slots=True)
class OwnHistory(HistoryView):
    """A thread that was not forked, or a fork that already holds its own copy of history."""


@dataclass(frozen=True, slots=True)
class ForkOwnHistory(HistoryView):
    """A fork's own history opens with its fork rows."""

    def framed(
        self, events: tuple[Event, ...], fork_rows: tuple[Event, ...], *,
        ends_at_fork: bool, reaches_start: bool,
    ) -> tuple[Event, ...]:
        return (*fork_rows, *events) if reaches_start else events


@dataclass(frozen=True, slots=True)
class ParentHistory(HistoryView):
    """A parent's history seen from a fork: only what the parent did before the fork, closed by
    the fork rows. The parent's later work is not the fork's history, so a page that reaches the
    parent's newest record still ends at the fork."""

    fork_birth: float

    def kept(self, events: tuple[Event, ...]) -> tuple[Event, ...]:
        return tuple(event for event in events if event.before(self.fork_birth))

    def framed(
        self, events: tuple[Event, ...], fork_rows: tuple[Event, ...], *,
        ends_at_fork: bool, reaches_start: bool,
    ) -> tuple[Event, ...]:
        return (*events, *fork_rows) if ends_at_fork else events
