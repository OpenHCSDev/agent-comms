"""Drop the rule pings stored before each carried its rule's type. A stored packet only records
which lines the heartbeat already raised, and these pings never recorded their rule, so nothing
can supply it; dropped, the heartbeat raises those lines once more in the declared shape.
Delete this step after the next installation.
"""

from __future__ import annotations

from contextlib import closing
from typing import ClassVar

from agent_comms.bus import Bus
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.messages import Message
from agent_comms.task_attachments.commander_pings import RulePing

# The paths of the stored rule pings without a rule in one task document.
UNTYPED = ("SELECT fullkey FROM json_tree({task}) WHERE type = 'object'"
           " AND json_extract(value, '$.kind') = :kind AND json_type(value, :rule) IS NULL")


class RulePingTypes(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (Bus,)

    def carry(self, *, apply: bool) -> Carried:
        table = Message.table
        column, = table._fields_named(("task",))
        names = {"kind": RulePing.declared_name, "rule": f"$.{FieldCodec.record_key(RulePing, 'rule')}"}
        rewritten = []
        changed = 0
        for path in self.input_files():
            with closing(Bus.read_snapshot(path)) as db:
                stored = db.execute(table._sql(
                    f'SELECT {{seq}}, {{task}} FROM "{table.declared_name}"'
                    f" WHERE EXISTS ({UNTYPED.format(task='{task}')})"), names).fetchall()
                updates: list[tuple[object, int]] = []
                for seq, task in stored:
                    # Later array elements go first, so each removal leaves the earlier paths valid.
                    paths = [found for found, in db.execute(
                        UNTYPED.format(task=":task") + " ORDER BY id DESC", {**names, "task": task})]
                    kept, = db.execute(f"SELECT json_remove(?{', ?' * len(paths)})",
                                       (task, *paths)).fetchone()
                    updates.append((kept, seq))
            changed += len(updates)
            if apply and updates:
                with closing(Bus.writer(path)) as db, Bus.write_transaction(db):
                    self.rewrite_stored(db, table, column, updates)
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, detail={"packets": changed}, rewritten=tuple(rewritten))
