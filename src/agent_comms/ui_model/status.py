"""Status lines for the threads open in views, read in one pass per Core revision.

Replaces a read per open view per coordination change: one pass reads every
open thread's presentation off the UI thread, and the model flushes the rows
that changed once per frame.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.thread_presentation import ThreadPresentation
from agent_comms.ui_model.changes import KeyedModel

NOTIFICATION_EXCERPT = 110


@dataclass(frozen=True)
class ThreadStatusModel:
    """What an open thread's status line shows."""

    name: str
    shown: bool
    lines: tuple[str, ...]
    working: bool
    attention: bool

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
        return cls(name, True, tuple(lines), presentation.busy, presentation.attention)

    @classmethod
    def unavailable(cls, name: str) -> ThreadStatusModel:
        return cls(name, True, ("Agent status unavailable",), False, True)


class OpenThreadStatus:
    """Status rows for the threads currently open in views."""

    # Core's read failures the line presents as "unavailable"; Core has no
    # common base for them yet, so these are the families its reads raise.
    UNAVAILABLE = (OSError, ValueError, RuntimeError)

    def __init__(self, schedule: Callable[[Callable[[], None]], None]):
        self.rows: KeyedModel[str, ThreadStatusModel] = KeyedModel(schedule)

    def read(self, views, names: Iterable[str]) -> tuple[dict[str, ThreadStatusModel], bool]:
        """Read each open thread's presentation (off the UI thread).

        Returns the rows and whether a busy store left some rows at their
        previous value, so the caller reads again on the next observation.
        """
        rows, retry = {}, False
        for name in names:
            try:
                rows[name] = ThreadStatusModel.known(name, views.thread_presentation(name))
            except CoordinationReadUnavailable:
                retry = True
                rows[name] = self.rows.rows.get(name) or ThreadStatusModel.unavailable(name)
            except self.UNAVAILABLE:
                rows[name] = ThreadStatusModel.unavailable(name)
        return rows, retry

    def apply(self, rows: dict[str, ThreadStatusModel]) -> None:
        self.rows.replace(rows)
