"""Date the external events saved before #2011 made every transcript event carry its time.

Those events are Claude tool calls; their Claude Code transcripts record when each
tool_use and tool_result happened. Each undated event takes that time; an event the
transcripts cannot date stops the carry, since readers no longer accept undated events.
"""
from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import datetime
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import ExternalEventRow, ExternalTranscript
from agent_comms.transcript_events import ToolEndTranscript, ToolStartTranscript

from .carry import Carried, StoreCarry

TRANSCRIPTS = Path.home() / ".claude" / "projects"
CALL = re.compile(r'"(?:id|tool_use_id)":"(toolu_\w+)"')
# Which transcript content item records each saved event kind.
RECORDED_BY = {"tool_use": (ToolStartTranscript.declared_name, "id"),
               "tool_result": (ToolEndTranscript.declared_name, "tool_use_id")}


def recorded_times(wanted: set[str]) -> dict[tuple[str, str], float]:
    """(kind, call id) to the time its transcript line records, for the wanted calls."""
    times: dict[tuple[str, str], float] = {}
    for path in TRANSCRIPTS.glob("**/*.jsonl"):
        with path.open(errors="replace") as lines:
            for line in lines:
                if "toolu_" not in line or not wanted.intersection(CALL.findall(line)):
                    continue
                record = json.loads(line)
                at = datetime.fromisoformat(record["timestamp"]).timestamp()
                for item in record["message"]["content"]:
                    kind, key = RECORDED_BY.get(item.get("type"), (None, None))
                    if kind and item[key] in wanted:
                        times.setdefault((kind, item[key]), at)
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
                f'SELECT DISTINCT kind, call_id FROM "{table}" WHERE timestamp IS NULL').fetchall()
            times = recorded_times({call for _, call in undated})
            missing = [event for event in undated if tuple(event) not in times]
            if missing:
                db.rollback()
                raise ValueError(f"{len(missing)} saved events have no transcript time, e.g. {missing[:3]}")
            for kind, call in undated:
                db.execute(f'UPDATE "{table}" SET event=json_set(event, \'$.timestamp\', ?) '
                           'WHERE timestamp IS NULL AND kind=? AND call_id=?',
                           (times[kind, call], kind, call))
            db.commit() if apply else db.rollback()
        return Carried(len(undated), {"events": len(undated)},
                       (path.name,) if apply and undated else ())


if __name__ == "__main__":
    ExternalEventTimes.main()
