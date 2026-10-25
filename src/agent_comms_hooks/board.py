"""The commander's board, printed on every tool call of its Claude session.

The Claude bridge writes the newest heartbeat packet's lines into its own delivery store
instead of handing them to a Monitor, whose events the harness frames as routine background
and which expires between turns. The session's tool-call hook reads them back here, with the
standard library only, and prints them on every call until the next packet replaces them.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, fields
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote


@dataclass(frozen=True)
class BoardLine:
    """One heartbeat line the commander disposes of: its text, when its condition began, and
    when it starts refusing the commander's writing verbs."""

    table: ClassVar[str] = "board_line"

    text: str
    since: float
    locks_at: float

    def aged(self, now: float) -> str:
        return f"{self.text} ({int((now - self.since) // 60)} min)"

    def line(self, now: float) -> str:
        locked = " -> LOCKS your writing verbs until a recorded action disposes of it"
        return f"BOARD: {self.aged(now)}{locked if now >= self.locks_at else ''}"

    @classmethod
    def recorded(cls, store: Path) -> tuple[BoardLine, ...]:
        """The lines the bridge recorded from its newest packet, oldest first."""
        columns = ", ".join(f'"{column.name}"' for column in fields(cls))
        # Path.as_uri imports urllib.request: 25 ms on every tool call.
        with closing(sqlite3.connect(f"file:{quote(str(store))}?mode=ro", uri=True)) as db:
            return tuple(
                cls(*row)
                for row in db.execute(f'SELECT {columns} FROM "{cls.table}" ORDER BY "since"')
            )
