"""Fold each thread's per-claim native cursor rows into its newest, keyed by the thread alone."""

from contextlib import closing
from pathlib import Path
from typing import ClassVar

from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.native_runtime_input import CurrentNativeCursor
from agent_comms.typed_table import SQLiteColumnShape


class NativeCursorThreadKey(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (CoordinationStore,)

    def carry(self, *, apply: bool) -> Carried:
        folded: dict[Path, list[CurrentNativeCursor]] = {}
        for path in self.input_files():
            with closing(CoordinationStore.read_snapshot(path)) as db:
                if not CurrentNativeCursor.present(db) or not SQLiteColumnShape.key_differs(db, CurrentNativeCursor):
                    continue
                folded[path] = self.retired(CurrentNativeCursor.select(db))
        if apply:
            for path, retired in folded.items():
                with closing(CoordinationStore.writer(path)) as db, CoordinationStore.write_transaction(db):
                    CurrentNativeCursor.release_guards(db)
                    for row in retired:
                        CurrentNativeCursor.delete(
                            db, wire_root_id=row.wire_root_id, recipient_lookup=row.recipient_lookup,
                            owner_generation=row.owner_generation,
                            owner_claimed_generation=row.owner_claimed_generation)
                    CurrentNativeCursor.rebuild(db)
        return Carried(
            len(folded),
            detail={"retired": sum(len(rows) for rows in folded.values())},
            rewritten=tuple(path.relative_to(self.root) for path in folded) if apply else (),
        )

    @staticmethod
    def retired(rows: list[CurrentNativeCursor]) -> list[CurrentNativeCursor]:
        """Every row but each thread's newest writer, which must also be its farthest."""
        threads: dict[tuple[str, str], list[CurrentNativeCursor]] = {}
        for row in rows:
            threads.setdefault((row.wire_root_id, row.recipient_lookup), []).append(row)
        retired = []
        for history in threads.values():
            history.sort(key=lambda row: (row.owner_generation, row.owner_claimed_generation))
            *older, newest = history
            if any(row.covered_seq > newest.covered_seq or row.injected_seq > newest.injected_seq
                   for row in older):
                raise RelationViolationError(
                    f"{newest.recipient_lookup}: an older claim's cursor covers more than the newest;"
                    " refusing to regress it"
                )
            retired.extend(older)
        return retired
