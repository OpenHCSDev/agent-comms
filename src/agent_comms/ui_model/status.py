"""Status lines for the threads open in views, from one observation per Core revision.

Replaces a read per open view per coordination change: the observation
service reads every open thread's presentation in its own process, and the
model flushes the rows that changed once per frame.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agent_comms.thread_presentation import (
    IdleThreadPresentationStatus,
    OtherRouteThread,
    SessionDisconnected,
    StatusPending,
    ThreadRetired,
    ThreadPresentation,
    ThreadPresentationStatus,
)
from agent_comms.ui_model.changes import KeyedModel
from agent_comms.ui_model.delivery import DeliveryView

if TYPE_CHECKING:
    from agent_comms.ui_model.observation import ThreadsObserved, UnreadableHistory

NOTIFICATION_EXCERPT = 110


@dataclass(frozen=True)
class ThreadStatusModel:
    """What an open thread's status line shows, and its one-line overview."""

    name: str
    shown: bool
    lines: tuple[str, ...]
    presentation: ThreadPresentation
    summary: tuple[str, ...] = ()
    """Overview parts for a collapsed status: first summary line, latest inbound, recent count."""

    @property
    def status(self) -> ThreadPresentationStatus:
        return self.presentation.status

    def with_delivery(self, delivery: DeliveryView) -> ThreadStatusModel:
        return replace(self, presentation=delivery.presentation(self.presentation))

    def disconnected(self, status: SessionDisconnected) -> ThreadStatusModel:
        return self.status.with_disconnection(
            self, lambda: ThreadStatusModel.known(self.name, status.over(self.presentation)))

    @classmethod
    def known(cls, name: str, presentation: ThreadPresentation) -> ThreadStatusModel:
        """Let the presentation member own its status-row projection."""
        presentation = presentation.for_status(name)
        lines = [presentation.summary, *(detail for detail in presentation.status.details
                                         if detail != presentation.summary)]
        excerpts = []
        for acknowledgement in presentation.notifications:
            message = acknowledgement.message
            if message is None:
                continue
            excerpt = " ".join(message.body.split())
            if len(excerpt) > NOTIFICATION_EXCERPT:
                excerpt = excerpt[:NOTIFICATION_EXCERPT - 3] + "…"
            excerpts.append(f"{message.target} · {message.sender}: {excerpt} — {acknowledgement.label}")
        if excerpts:
            lines += ["Recent incoming messages:", *excerpts]
        summary = [presentation.summary.partition("\n")[0] or "Ready"]
        latest = next((acknowledgement for acknowledgement in presentation.notifications if acknowledgement.message is not None), None)
        if latest is not None:
            summary.append(f"Latest inbound {latest.message.target} from @{latest.message.sender}: {latest.label}")
        if len(presentation.notifications) > 1:
            summary.append(f"{len(presentation.notifications)} recent")
        return cls(name, True, tuple(lines), presentation,
                   summary=tuple(summary))

    @classmethod
    def pending(cls, name: str) -> ThreadStatusModel:
        return cls.known(name, StatusPending().presentation(name))

    @classmethod
    def retired(cls, name: str) -> ThreadStatusModel:
        return cls.known(name, ThreadRetired().presentation(name))

    @classmethod
    def other_route(cls, name: str) -> ThreadStatusModel:
        return cls.known(name, OtherRouteThread().presentation(name))

    @classmethod
    def unreadable(cls, name: str, error: UnreadableHistory) -> ThreadStatusModel:
        return cls.known(name, ThreadPresentation(
            name, "!", str(error), status=IdleThreadPresentationStatus().with_attention(str(error))))


class OpenThreadStatus:
    """Status rows for the threads currently open in views."""

    def __init__(self, schedule: Callable[[Callable[[], None]], None]):
        self.rows: KeyedModel[str, ThreadStatusModel] = KeyedModel(schedule)

    def open(self, name: str) -> ThreadStatusModel:
        """Register the first observation gap; every open view holds a real member."""
        if name not in self.rows.rows:
            self.rows.replace({**self.rows.rows, name: ThreadStatusModel.pending(name)})
        return self.rows.rows[name]

    def observed(self, result: ThreadsObserved) -> None:
        """Publish one observation of the open threads.

        A thread whose store was busy keeps its previous row, or its initial
        StatusPending member until the service completes its first read.
        """
        rows = {name: row for name, row in self.rows.rows.items() if name not in result.changed}
        rows.update((name, answer.status_row(name, self)) for name, answer in result.results.items())
        self.rows.replace(rows)

    def clear(self) -> None:
        self.rows.replace({})
