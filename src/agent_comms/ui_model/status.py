"""Status lines for the threads open in views, from one observation per Core revision.

Replaces a read per open view per coordination change: the observation
service reads every open thread's presentation in its own process, and the
model flushes the rows that changed once per frame.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.thread_presentation import ThreadPresentation
from agent_comms.ui_model.changes import KeyedModel

if TYPE_CHECKING:
    from agent_comms.ui_model.observation import ThreadsObserved

NOTIFICATION_EXCERPT = 110


@dataclass(frozen=True)
class ThreadStatusModel:
    """What an open thread's status line shows, and its one-line overview."""

    name: str
    shown: bool
    lines: tuple[str, ...]
    working: bool
    attention: bool
    available: bool = True
    summary: tuple[str, ...] = ()
    """Overview parts for a collapsed status: first summary line, latest inbound, recent count."""

    @classmethod
    def known(cls, name: str, presentation: ThreadPresentation | None) -> ThreadStatusModel:
        if presentation is None:
            return cls(name, False, ("",), False, False)
        lines = [presentation.summary]
        excerpts = []
        for receipt in presentation.notifications:
            message = receipt.message
            if message is None:
                continue
            excerpt = " ".join(message.body.split())
            if len(excerpt) > NOTIFICATION_EXCERPT:
                excerpt = excerpt[:NOTIFICATION_EXCERPT - 3] + "…"
            excerpts.append(f"{message.target} · {message.sender}: {excerpt} — {receipt.state}")
        if excerpts:
            lines += ["Recent incoming messages:", *excerpts]
        summary = [presentation.summary.partition("\n")[0] or "Ready"]
        latest = next((receipt for receipt in presentation.notifications if receipt.message is not None), None)
        if latest is not None:
            summary.append(f"Latest inbound {latest.message.target} from @{latest.message.sender}: {latest.state}")
        if len(presentation.notifications) > 1:
            summary.append(f"{len(presentation.notifications)} recent")
        return cls(name, True, tuple(lines), presentation.busy, presentation.attention,
                   summary=tuple(summary))

    @classmethod
    def unavailable(cls, name: str) -> ThreadStatusModel:
        return cls(name, True, ("Agent status unavailable",), False, True,
                   available=False, summary=("Status unavailable",))


class OpenThreadStatus:
    """Status rows for the threads currently open in views."""

    def __init__(self, schedule: Callable[[Callable[[], None]], None]):
        self.rows: KeyedModel[str, ThreadStatusModel] = KeyedModel(schedule)

    def observed(self, result: ThreadsObserved) -> None:
        """Publish one observation of the open threads.

        A thread whose store was busy keeps its previous row (or shows
        unavailable until a first read succeeds); the service reads it again.
        """
        rows = {name: ThreadStatusModel.known(name, presentation)
                for name, presentation in result.presentations.items()}
        rows.update((name, ThreadStatusModel.unavailable(name)) for name in result.unavailable)
        rows.update((name, self.rows.rows.get(name) or ThreadStatusModel.unavailable(name))
                    for name in result.busy)
        self.rows.replace(rows)

    def clear(self) -> None:
        self.rows.replace({})
