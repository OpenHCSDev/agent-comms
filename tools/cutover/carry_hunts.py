"""Move retained hunts out of problem rows into the commander's hunt records.

A clean inspection is not a problem. Both outcomes keep their original time,
source and reason; no order, request or current clock is invented.
"""
from dataclasses import dataclass
import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.todos import Hunt, HuntRecord
from agent_comms.typed_table import TypedRow
from agent_comms.installer import carry


@dataclass(frozen=True)
class SavedHunts(TypedRow):
    id: str
    creator: str
    creator_created: float
    hunts: str


class HuntRecords(carry.TodoRows):

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> carry.Carried:
        moved = 0
        schema = 0
        with cls.todo_rows(root, apply=apply) as db:
            if db is not None:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if HuntRecord.declared_name not in tables:
                    HuntRecord.create(db)
                    schema += 1
                columns = {row[1] for row in db.execute('PRAGMA table_info(todo)')}
                if 'hunts' in columns:
                    for row in SavedHunts.read(db.execute('SELECT id,creator,creator_created,hunts FROM todo')):
                        hunts = FieldCodec.decode(tuple[Hunt, ...], json.loads(row.hunts))
                        for index, hunt in enumerate(hunts):
                            HuntRecord(f'{row.id}:{index}',
                                RecordedThreadIncarnation(row.creator, row.creator_created), hunt).upsert(db)
                            moved += 1
                    db.execute('ALTER TABLE todo DROP COLUMN hunts')
                    schema += 1
        return cls.carried(root, moved + schema, {'hunts': moved, 'schema': schema}, apply=apply)
