"""Date the external events saved before #2011 made every transcript event carry its time.

Those events are Claude tool calls. Replaying each Claude Code transcript through the
bridge's own reader yields the same events with their recorded times; each undated row
takes its event's time. An event no transcript dates stops the carry, since readers no
longer accept undated events.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from agent_comms.cli_bridges.claude_protocol import (
    CLAUDE_RECORD,
    AssistantRecord,
    ToolResult,
    ToolUse,
    UserRecord,
)
from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import ExternalEventRow, ExternalTranscript
from agent_comms.transcript_events import ToolEndTranscript, ToolStartTranscript

from .carry import Carried, StoreCarry

TRANSCRIPTS = Path.home() / ".claude" / "projects"
CALL = re.compile(r'"(?:id|tool_use_id)":"(toolu_\w+)"')
# Claude's own block types, and which saved event each one dates.
BLOCKS: tuple[tuple[type[ToolUse | ToolResult], str, Callable[..., str]], ...] = (
    (ToolUse, ToolStartTranscript.declared_name, lambda block: block.id),
    (ToolResult, ToolEndTranscript.declared_name, lambda block: block.tool_use_id),
)


def recorded_times(wanted: set[str]) -> dict[tuple[str, str], float]:
    """(event kind, call id) to the time its transcript line records, for the wanted calls.
    The bridge's reader replays only a recent window, so every line is decoded here with
    the bridge's record decoder."""
    times: dict[tuple[str, str], float] = {}
    for path in TRANSCRIPTS.glob("**/*.jsonl"):
        with path.open(errors="replace") as lines:
            for position, line in enumerate(lines):
                if "toolu_" not in line or not wanted.intersection(CALL.findall(line)):
                    continue
                try:
                    record = CLAUDE_RECORD.validate_python(
                        {**json.loads(line), "position": position}
                    )
                except ValidationError:
                    continue  # snapshots and queue lines quote call ids but are no timed record
                if not isinstance(record, AssistantRecord | UserRecord):
                    continue
                for block in record.message.content:
                    for decoded, kind, call_of in BLOCKS:
                        if isinstance(block, decoded) and call_of(block) in wanted:
                            times.setdefault((kind, call_of(block)), record.timestamp)
    return times


class ExternalEventTimes(StoreCarry):
    store = ExternalTranscript

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = cls.store.path_in(root)
        if not path.exists():
            return Carried(0)
        table = ExternalEventRow.declared_name
        with closing(CoordinationStore.writer(path)) as db:
            db.execute("BEGIN IMMEDIATE")
            undated = db.execute(
                f'SELECT DISTINCT kind, call_id FROM "{table}" WHERE timestamp IS NULL'
            ).fetchall()
            times = recorded_times({call for _, call in undated})
            missing = [event for event in undated if tuple(event) not in times]
            if missing:
                db.rollback()
                raise ValueError(
                    f"{len(missing)} saved events have no transcript time, e.g. {missing[:3]}"
                )
            # One pass over the table: per-event updates would each scan it,
            # since no index leads with call_id.
            db.execute(
                "CREATE TEMP TABLE dated"
                " (kind TEXT, call_id TEXT, at REAL, PRIMARY KEY (kind, call_id))"
            )
            db.executemany(
                "INSERT INTO dated VALUES (?, ?, ?)",
                ((kind, call, at) for (kind, call), at in times.items()),
            )
            db.execute(
                f"UPDATE \"{table}\" AS t SET event=json_set(event, '$.timestamp',"
                " (SELECT at FROM dated d WHERE d.kind=t.kind AND d.call_id=t.call_id))"
                " WHERE timestamp IS NULL"
            )
            db.commit() if apply else db.rollback()
        return Carried(
            len(undated), {"events": len(undated)}, (path.name,) if apply and undated else ()
        )


if __name__ == "__main__":
    ExternalEventTimes.main()
