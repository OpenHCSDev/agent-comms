"""Give every stored triage membership row the replacement attempt it belongs to.

The membership relation gained an attempt so one claim can own the original ask and
the one replacement an unparseable decision earns. Every row written before that is
the original ask, so a carried row is attempt 0.
"""

from contextlib import closing
from typing import ClassVar

from agent_comms.coordination.database import CoordinationStore
from agent_comms.execution.triage_native_sources import TriageNativeSource
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.storage.locked_store import RootFile


class TriageAttemptColumn(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (CoordinationStore,)
    added: ClassVar[str] = "attempt"

    def carry(self, *, apply: bool) -> Carried:
        upgraded = []
        for path in self.input_files():
            with closing(CoordinationStore.read_snapshot(path)) as db:
                if not TriageNativeSource.present(db):
                    continue
                if self.added in TriageNativeSource.stored_columns(db):
                    continue
                upgraded.append(path)
        if apply:
            for path in upgraded:
                with (
                    closing(CoordinationStore.writer(path)) as db,
                    CoordinationStore.write_transaction(db),
                ):
                    TriageNativeSource.rebuild(db, changed={self.added: "0"})
        return Carried(
            len(upgraded),
            detail={"stores": len(upgraded)},
            rewritten=tuple(path.relative_to(self.root) for path in upgraded) if apply else (),
        )
