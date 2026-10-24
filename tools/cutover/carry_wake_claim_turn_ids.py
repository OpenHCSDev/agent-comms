"""Validate version-one wake turn identities before stamping their narrowed format."""

import sys
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import ClassVar

from agent_comms.bus import Bus
from agent_comms.envelope_claim_transitions import WakeClaim
from agent_comms.field_codec import FieldCodec, TurnId
from agent_comms.historical_views import HistoryArchive
from agent_comms.installer.probes.carry import ArchivedInputs, Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.messages import Claimed


class WakeClaimTurnIds(ArchivedInputs, Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (Bus,)

    def roots(self) -> tuple[Path, ...]:
        roots = [self.root]
        for root in roots:
            self.require_confined(self.root, root)
            roots.extend(HistoryArchive.archived_roots(root))
        return tuple(roots)

    @staticmethod
    def rewrite(path: Path, record: type, payload: dict[str, object]) -> dict[str, object]:
        if record is not WakeClaim or payload["version"] != 1:
            return payload
        value = payload["turn_id"]
        try:
            turn_id = FieldCodec.decode(TurnId, value)
        finally:
            # Keep the codec's refusal and traceback; add the old file/value without catching it.
            if (error := sys.exception()) is not None:
                error.add_note(f"{path}: WakeClaim.turn_id={value!r}")
        return {
            **payload, "turn_id": FieldCodec.encode(turn_id, TurnId), "version": WakeClaim.version,
        }

    def carry(self, *, apply: bool) -> Carried:
        table = Claimed.table
        column, = table._fields_named(("claim_transition",))
        changed = 0
        rewritten: list[Path] = []
        for path in self.input_files():
            updates: list[tuple[object, int]] = []
            with closing(Bus.read_snapshot(path)) as db:
                if not table.present(db):
                    continue
                for seq, stored in db.execute(table._sql(
                    f'SELECT {{seq}}, {{claim_transition}} FROM "{table.declared_name}"'
                )):
                    original = column.storage.from_sql(stored)
                    current = column.value.rewrite_document(
                        original, partial(self.rewrite, path), lambda record: record is WakeClaim,
                    )
                    if current != original:
                        updates.append((column.storage.to_sql(current), seq))
            changed += len(updates)
            if apply and updates:
                with closing(Bus.writer(path)) as db, Bus.write_transaction(db):
                    db.executemany(table._sql(
                        f'UPDATE "{table.declared_name}" SET {{claim_transition}}=? WHERE {{seq}}=?'
                    ), updates)
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, rewritten=tuple(rewritten))
