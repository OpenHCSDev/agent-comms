"""Rewrite pre-P566 saved events with their original item identity.

Each finished event becomes one indexed row in its existing transcript store.
The old array column is deleted. Item sequences, bodies, routes and times stay
unchanged. Current bridge writers already supply position; readers stay strict.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import DraftRow, ExternalEventRow, ExternalItemRow, ExternalTranscript
from agent_comms.field_codec import FieldCodec
from agent_comms.transcript_events import TranscriptEvent

from agent_comms.installer import carry
from agent_comms.installer.carry import Carried


def located(encoded, item_id):
    events = FieldCodec.parse(encoded)
    changed = 0
    for event in events:
        member, _ = FieldCodec.record_member(TranscriptEvent, event)
        if "position" not in event and any(
            key == "position" for _, key in FieldCodec.record_fields(member)
        ):
            event["position"] = item_id
            changed += 1
    return FieldCodec.decode(tuple[TranscriptEvent, ...], events), changed


class ExternalPositions(carry.StoreCarry):
    """Saved events take the identity their original bridge item owns."""

    store = ExternalTranscript

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = ExternalTranscript.path_in(root)
        if not path.exists():
            return Carried(0)
        changed = positions = events_count = 0
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as source:
            tables = {name for name, in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if ExternalEventRow.declared_name in tables:
                # A second run checks the new declaration without rewriting it.
                for table in ExternalTranscript.rows:
                    for _ in table.stream(source):
                        pass
                return Carried(0)
            expected = {ExternalItemRow.declared_name, DraftRow.declared_name}
            if tables != expected:
                raise ValueError(f"Unexpected external transcript tables: {tables}")
            descriptor, temporary = tempfile.mkstemp(prefix=".external-positions-", dir=root)
            os.close(descriptor)
            stage = Path(temporary)
            try:
                with closing(CoordinationStore.writer(stage)) as target:
                    target.execute("PRAGMA journal_mode=DELETE")
                    for table in ExternalTranscript.rows:
                        table.create(target)
                    target.execute("BEGIN IMMEDIATE")
                    try:
                        # Decode metadata independently of the old event array.
                        for item in ExternalItemRow.stream(source):
                            encoded, = source.execute(
                                f'SELECT events FROM "{ExternalItemRow.declared_name}" '
                                'WHERE thread=? AND seq=?', (item.thread, item.seq)).fetchone()
                            events, missing = located(encoded, item.item_id)
                            item.insert(target)
                            ExternalEventRow.append(target, item.thread, item.seq, events)
                            changed += 1
                            positions += missing
                            events_count += len(events)
                        for thread, turn, item, encoded in source.execute(
                            f'SELECT thread,turn_id,item_id,events FROM "{DraftRow.declared_name}"'
                        ):
                            events, missing = located(encoded, item)
                            DraftRow(thread, turn, item, events).insert(target)
                            positions += missing
                        target.commit()
                    except BaseException:
                        target.rollback()
                        raise
                if apply:
                    with stage.open("rb") as ready:
                        os.fsync(ready.fileno())
                    # Normal carry application runs after the bridge is stopped.
                    # Its SQLite backup is made by the existing update owner.
                    os.replace(stage, path)
                    for suffix in ("-wal", "-shm"):
                        path.with_name(path.name + suffix).unlink(missing_ok=True)
                    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            finally:
                stage.unlink(missing_ok=True)
                for suffix in ("-journal", "-wal", "-shm"):
                    stage.with_name(stage.name + suffix).unlink(missing_ok=True)
        return Carried(changed, {"positions": positions, "events": events_count},
                       (path.name,) if apply else ())
