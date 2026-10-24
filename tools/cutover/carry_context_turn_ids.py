"""Unbox recorded context turn identities before the nominal reader opens them."""

from contextlib import closing
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import ClassVar

from agent_comms.context_store import ContextRequest, ContextStore
from agent_comms.field_codec import FieldCodec, MalformedRecord, TurnId
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.turn_context import RecordedContextTurn


@dataclass(frozen=True)
class StoredRegisteredTurn:
    value: TurnId


class ContextTurnIds(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (ContextStore,)

    @staticmethod
    def rewrite(path: Path, record: type, payload: dict[str, object]) -> dict[str, object]:
        try:
            identity = payload["identity"]
            current = payload if isinstance(identity, str) else {
                **payload,
                "identity": FieldCodec.encode(
                    FieldCodec.decode(StoredRegisteredTurn, identity).value
                ),
            }
            FieldCodec.decode(RecordedContextTurn, current)
            return current
        except MalformedRecord as error:
            error.add_note(f"{path}: {payload!r}")
            raise

    def carry(self, *, apply: bool) -> Carried:
        table = ContextRequest
        (column,) = table._fields_named(("identity",))
        changed = 0
        rewritten: list[Path] = []
        for path in self.input_files():
            updates: list[tuple[object, int]] = []
            with closing(ContextStore.read_snapshot(path)) as db:
                if not table.present(db):
                    continue
                for key, stored in db.execute(
                    table._sql(f'SELECT {{id}}, {{identity}} FROM "{table.declared_name}"')
                ):
                    original = column.storage.from_sql(stored)
                    current = column.value.rewrite_document(
                        original,
                        partial(self.rewrite, path),
                        lambda record: record is RecordedContextTurn,
                    )
                    if current != original:
                        updates.append((column.storage.to_sql(current), key))
            changed += len(updates)
            if apply and updates:
                with closing(ContextStore.writer(path)) as db, ContextStore.write_transaction(db):
                    self.rewrite_stored(db, table, column, updates)
                rewritten.append(path.relative_to(self.root))
        return Carried(changed, rewritten=tuple(rewritten))
