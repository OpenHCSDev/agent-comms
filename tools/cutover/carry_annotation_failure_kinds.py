"""Carry annotation failures stored as an error class's name into the failure family."""

from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import ClassVar

from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.tables.annotations import AnnotationRequestsRow
from agent_comms.errors import RelationViolationError
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.storage.locked_store import RootFile
from agent_comms.working_memory.working_memory_requests import FailedAnnotationOutcome


class AnnotationFailureKinds(Carry):
    """A failed request stored `{"kind": "failed", "error": NAME}`, NAME being the caught
    error's class. The class picks the family member that clause now records."""

    opened_files: ClassVar[tuple[type[RootFile], ...]] = (CoordinationStore,)

    @staticmethod
    def errors_named() -> dict[str, set[type[BaseException]]]:
        named: dict[str, set[type[BaseException]]] = {}
        pending = list(FailedAnnotationOutcome.caught_errors())
        while pending:
            error = pending.pop()
            named.setdefault(error.__name__, set()).add(error)
            pending.extend(error.__subclasses__())
        return named

    def carry(self, *, apply: bool) -> Carried:
        (column,) = AnnotationRequestsRow._fields_named(("outcome",))
        named = self.errors_named()
        counts: Counter[str] = Counter()
        rewritten: list[Path] = []
        for path in self.input_files():
            with closing(CoordinationStore.read_snapshot(path)) as db:
                if not AnnotationRequestsRow.present(db):
                    continue
                stored = db.execute(
                    "SELECT id, json_extract(outcome, '$.error') "
                    f'FROM "{AnnotationRequestsRow.declared_name}" '
                    "WHERE json_extract(outcome, '$.kind') = 'failed'"
                ).fetchall()
            updates: list[tuple[object, object]] = []
            for row_id, name in stored:
                errors = named.get(name, set())
                if len(errors) != 1:
                    raise RelationViolationError(
                        f"{path}: annotation request {row_id} stored error {name!r}, which names "
                        f"{len(errors)} caught error classes")
                (error,) = errors
                outcome = FailedAnnotationOutcome.for_error(error)
                counts.update((outcome.declared_name,))
                updates.append((column.encode(outcome), row_id))
            if apply and updates:
                with closing(CoordinationStore.writer(path)) as db, CoordinationStore.write_transaction(db):
                    self.rewrite_stored(db, AnnotationRequestsRow,
                                        FieldCodec.field_plan(AnnotationRequestsRow, column.name), updates)
                rewritten.append(path.relative_to(self.root))
        return Carried(sum(counts.values()), detail=dict(counts), rewritten=tuple(rewritten))
