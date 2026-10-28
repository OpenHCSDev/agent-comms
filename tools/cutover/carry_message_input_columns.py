"""Move a message's input facts from bus_message into MessageInput's own capability table.

The moved columns' stored values are copied unchanged, so every message decodes to the same
value and keeps its content identity; reconciliation would otherwise drop them.
"""

from contextlib import closing
from pathlib import Path
from typing import ClassVar

from agent_comms.bus import Bus
from agent_comms.errors import RelationViolationError
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.messages import Message, MessageInput


class MessageInputColumns(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (Bus,)

    @staticmethod
    def moved_columns() -> str:
        """Every column MessageInput's table declares, its sequence included."""
        table = MessageInput.table
        return table._column_list(tuple(item.name for item in table._writable_fields()))

    @staticmethod
    def pending(stored: tuple[str, ...]) -> bool:
        """Whether bus_message still holds a column that only MessageInput now declares."""
        kept = {item.wire_name for item in Message.table._writable_fields()}
        return any(item.wire_name in stored and item.wire_name not in kept
                   for item in MessageInput.table._writable_fields())

    def carry(self, *, apply: bool) -> Carried:
        source, target = Message.table, MessageInput.table
        columns = self.moved_columns()
        counted: dict[Path, int] = {}
        for path in self.input_files():
            with closing(Bus.read_snapshot(path)) as db:
                if not source.present(db) or not self.pending(source.stored_columns(db)):
                    continue
                if target.present(db):
                    raise RelationViolationError(
                        f"{path}: {target.declared_name} exists while {source.declared_name} "
                        "still holds its columns; refusing to choose between them")
                (counted[path],) = db.execute(
                    f'SELECT count(*) FROM "{source.declared_name}"').fetchone()
        rewritten: list[Path] = []
        if apply:
            for path, count in counted.items():
                with closing(Bus.writer(path)) as db, Bus.write_transaction(db):
                    target.create(db)
                    db.execute(f'INSERT INTO "{target.declared_name}" ({columns}) '
                               f'SELECT {columns} FROM "{source.declared_name}"')
                    (moved,) = db.execute(
                        f'SELECT count(*) FROM "{target.declared_name}"').fetchone()
                    (differing,) = db.execute(
                        f'SELECT count(*) FROM (SELECT {columns} FROM "{source.declared_name}" '
                        f'EXCEPT SELECT {columns} FROM "{target.declared_name}")').fetchone()
                    if (moved, differing) != (count, 0):
                        raise RelationViolationError(
                            f"{path}: moved {moved} of {count} messages, {differing} differ")
                    source.rebuild(db)
                rewritten.append(path.relative_to(self.root))
        return Carried(len(counted), detail={"messages": sum(counted.values())},
                       rewritten=tuple(rewritten))
