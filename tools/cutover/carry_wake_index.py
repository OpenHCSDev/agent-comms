"""The wake index's rows in the wake-policy declaration that adds 'unmentioned'.

The only declared difference is that policy value, so every saved row decodes
unchanged. An index already in the current declaration has nothing to carry.
The saved rows belong to one bus file: on a copy or restore of the bus the index
starts empty in the current declaration and its reader builds it.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.store_files import _store_lock
from agent_comms.wake_candidate_index import (CandidateCheckpoint, ProjectionRebuildRequiredError,
                                              WakeCandidateIndex)
from agent_comms.wire_log import CurrentSnapshotOpening, WireLog

from cutover.carry import Carried, Carry


class WakePolicyUnmentioned(Carry):
    """Wake rows rewritten under the expanded wake-policy declaration."""

    after = "carry_route_index"

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        log = WireLog(root / "bus.jsonl")
        index = WakeCandidateIndex(log)
        if not index.path.exists():
            return Carried(0)
        with closing(CoordinationStore.read_snapshot(index.path)) as db:
            expected = index.declared_schema()
            actual = {item.name: item.sql for item in index._schema(db)}
            if actual == expected:
                return Carried(0)
            if actual.keys() != expected.keys():
                raise ValueError("Wake index has unknown schema objects")
            for name, sql in actual.items():
                expanded = sql.replace("'passive', 'bounded_triage', 'full'",
                                       "'passive', 'unmentioned', 'bounded_triage', 'full'")
                if expanded != expected[name]:
                    raise ValueError(f"Unexpected wake table layout: {name}")
            # Each select decodes through the declared table codec before any write.
            rows = {owner: owner.select(db) for owner in index.rows}
            saved = CandidateCheckpoint.current(db)
        with log._opened_wire_snapshot(opening=CurrentSnapshotOpening()) as snapshot:
            try:
                saved.require_source(snapshot, snapshot.metadata.root_id)
            except ProjectionRebuildRequiredError:
                # Rows saved for another bus file (a copy, a restore) are not this
                # bus's: the index starts empty and its reader builds it.
                rows = {owner: [] for owner in rows}
        if apply:
            with _store_lock(index.path), closing(CoordinationStore.writer(index.path)) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    for owner in reversed(index.rows):
                        owner.drop(db)
                    for owner, values in rows.items():
                        owner.create(db)
                        for value in values:
                            value.insert(db)
                    index.require_schema(index._schema(db))
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
        detail = {owner.declared_name: len(values) for owner, values in rows.items()}
        rewritten = tuple(path.name for path in root.glob(f"{index.path.name}*")) if apply else ()
        return Carried(1, detail, rewritten)


if __name__ == "__main__":
    WakePolicyUnmentioned.main()
