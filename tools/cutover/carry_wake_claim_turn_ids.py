"""Validate version-one wake turn identities before stamping their narrowed format."""

from contextlib import closing
from functools import partial
from pathlib import Path
from typing import ClassVar

from agent_comms.bus import Bus
from agent_comms.envelope_claim_transitions import WakeClaim
from agent_comms.field_codec import FieldCodec, MalformedRecord
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
        try:
            return {
                **FieldCodec.encode(
                    FieldCodec.decode(WakeClaim, {**payload, "version": WakeClaim.version})
                )
            }
        except MalformedRecord as error:
            error.add_note(f"{path}: {payload!r}")
            raise

    def carry(self, *, apply: bool) -> Carried:
        table = Claimed.table
        (column,) = table._fields_named(("claim_transition",))
        changed = 0
        rewritten: list[Path] = []
        for path in self.input_files():
            updates: list[tuple[object, int]] = []
            with closing(Bus.read_snapshot(path)) as db:
                if not table.present(db):
                    continue
                for seq, stored in db.execute(
                    table._sql(f'SELECT {{seq}}, {{claim_transition}} FROM "{table.declared_name}"')
                ):
                    original = column.storage.from_sql(stored)
                    current = column.value.rewrite_document(
                        original,
                        partial(self.rewrite, path),
                        lambda record: record is WakeClaim,
                    )
                    if current != original:
                        updates.append((column.storage.to_sql(current), seq))
            changed += len(updates)
            if apply and updates:
                with closing(Bus.writer(path)) as db, Bus.write_transaction(db):
                    self.rewrite_stored(db, table, column, updates)
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, rewritten=tuple(rewritten))
