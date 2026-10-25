"""Drop the retired HUD rows from stored commander updates and give each decisions packet its
undisposed tail. The HUD owns those rows now; the stored packets keep only heartbeat lines.
Delete this step after the live installation.
"""

from __future__ import annotations

import json
from contextlib import closing
from typing import ClassVar

from agent_comms.bus import Bus
from agent_comms.commander_pings import CommanderDecisions, CommanderNotice, Ping
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.messages import Message

RETIRED = frozenset(("load", "unit_row", "phase_row", "finished_phases_row", "forks_row"))
LINES = ("handled", "decisions")
TAIL = FieldCodec.record_key(CommanderDecisions, "waiting")


def carried(wire: dict[str, object]) -> dict[str, object]:
    """The stored packet without retired rows, decoding as its declared update."""
    kept = {key: [ping for ping in value if ping["kind"] not in RETIRED]
            if key in LINES else value for key, value in wire.items()}
    if wire["kind"] == CommanderDecisions.declared_name:
        kept.setdefault(TAIL, [])
    FieldCodec.decode(tuple[Ping, ...], kept["handled"])
    return kept


class CommanderUpdateLines(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (Bus,)

    def carry(self, *, apply: bool) -> Carried:
        table = Message.table
        column, = (item for item in table._fields if item.name == "task")
        kinds = (CommanderDecisions.declared_name, CommanderNotice.declared_name)
        rewritten = []
        changed = 0
        for path in self.input_files():
            with closing(Bus.read_snapshot(path)) as db:
                stored = db.execute(table._sql(
                    f'SELECT {{seq}}, {{task}} FROM "{table.declared_name}"'
                    " WHERE json_extract({task}, '$.kind') IN (?, ?)"), kinds).fetchall()
            wires = ((seq, json.loads(task)) for seq, task in stored)
            updates = [(json.dumps(kept), seq) for seq, wire in wires
                       if (kept := carried(wire)) != wire]
            changed += len(updates)
            if apply and updates:
                with closing(Bus.writer(path)) as db, Bus.write_transaction(db):
                    self.rewrite_stored(db, table, column, updates)
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, detail={"packets": changed}, rewritten=tuple(rewritten))
