"""Carry automatic strike releases into ordinary retained release history.

Only the event's retired wire tag changes; actor, assignment, reason and time stay.
Delete this step after the live installation has carried its retained rows.
"""
import json
from contextlib import closing
from dataclasses import dataclass
from typing import ClassVar

from pydantic import JsonValue

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.todos import ReleaseTodo, StoredOrderStory, TodoCommand, TodoStore
from agent_comms.typed_table import TypedRow


@dataclass(frozen=True)
class StoredRelease(TypedRow):
    seq: int
    event: dict[str, JsonValue]


class StrikeReleases(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (TodoStore,)

    def carry(self, *, apply: bool) -> Carried:
        path = TodoStore.path_in(self.root)
        self.require_confined(path)
        if not path.exists():
            return Carried(0)
        with closing(TodoStore.read_snapshot(path)) as db:
            if not StoredOrderStory.present(db):
                return Carried(0)
            rows = StoredOrderStory.select(db, row_type=StoredRelease, kind="limit_release_todo")
        updates = []
        for row in rows:
            del row.event[TodoCommand.family_discriminator]
            release = FieldCodec.decode_record(ReleaseTodo, row.event)
            updates.append((json.dumps(FieldCodec.encode(release)), row.seq))
        if apply and updates:
            column = FieldCodec.field_plan(StoredOrderStory, "event")
            with closing(TodoStore.writer(path)) as db, TodoStore.write_transaction(db):
                self.rewrite_stored(db, StoredOrderStory, column, updates)
        return Carried(len(updates), detail={"releases": len(updates)},
                       rewritten=(path.relative_to(self.root),) if apply and updates else ())
