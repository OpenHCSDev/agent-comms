"""Split each triage input's JSON membership array into one indexed row per member."""

from contextlib import closing
from pathlib import Path
from typing import ClassVar

from agent_comms.coordination_database import CoordinationStore
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.triage_native_sources import TriageNativeSource
from agent_comms.typed_table import SQLiteCatalogObject


class TriageMemberRows(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (CoordinationStore,)
    previous: ClassVar[str] = "native_runtime_triage_sources"

    def carry(self, *, apply: bool) -> Carried:
        split: dict[Path, list[TriageNativeSource]] = {}
        for path in self.input_files():
            with closing(CoordinationStore.read_snapshot(path)) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                  (self.previous,)).fetchone():
                    continue
                split[path] = TriageNativeSource.read(db.execute(
                    f'SELECT s.input_id, CAST(member.key AS INTEGER) AS ordinal, member.value AS assignment_id '
                    f'FROM "{self.previous}" s, json_each(s.assignment_ids) member'))
        if apply:
            for path, members in split.items():
                with closing(CoordinationStore.writer(path)) as db, CoordinationStore.write_transaction(db):
                    TriageNativeSource.create(db, if_not_exists=True)
                    SQLiteCatalogObject.release_guards(db, TriageNativeSource)
                    for member in members:
                        member.insert(db)
                    for sql in TriageNativeSource.triggers().values():
                        db.execute(sql)
                    db.execute(f'DROP TABLE "{self.previous}"')
        return Carried(
            len(split),
            detail={"members": sum(len(members) for members in split.values())},
            rewritten=tuple(path.relative_to(self.root) for path in split) if apply else (),
        )
