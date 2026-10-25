"""Carry automatic strike releases into ordinary retained release history.

Only the event's retired wire tag changes; actor, assignment, reason and time stay.
Delete this step after the live installation has carried its retained rows.
"""
from contextlib import closing
from typing import ClassVar

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.todos import ReleaseTodo, StoredOrderStory, TodoCommand, TodoStore


class StrikeReleases(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (TodoStore,)

    def carry(self, *, apply: bool) -> Carried:
        path = TodoStore.path_in(self.root)
        self.require_confined(path)
        if not path.exists():
            return Carried(0)
        kind = f"$.{TodoCommand.family_discriminator}"
        with closing(TodoStore.read_snapshot(path)) as db:
            if not StoredOrderStory.present(db):
                return Carried(0)
            updates = db.execute(StoredOrderStory._sql(
                f'SELECT json_set({{event}}, ?, ?), {{seq}} FROM "{StoredOrderStory.declared_name}" '
                'WHERE json_extract({event}, ?) = ?'),
                (kind, ReleaseTodo.declared_name, kind, "limit_release_todo")).fetchall()
        if apply and updates:
            column, = StoredOrderStory._fields_named(("event",))
            with closing(TodoStore.writer(path)) as db, TodoStore.write_transaction(db):
                self.rewrite_stored(db, StoredOrderStory, column, updates)
        return Carried(len(updates), detail={"releases": len(updates)},
                       rewritten=(path.relative_to(self.root),) if apply and updates else ())
