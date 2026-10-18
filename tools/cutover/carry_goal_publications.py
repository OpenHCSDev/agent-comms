"""Goal publication sequences (P257).

Republish legacy registry and journal goals at the certified wire head. Earlier
UNKNOWN input rows remain untouched.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing, nullcontext
from pathlib import Path

import agent_comms.private_bus_checkpoint as private_bus_checkpoint
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_history import GoalHistoryEntry
from agent_comms.goals import Goal
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import _store_lock
from agent_comms.wire_log import WireLog

from cutover.carry import Carried, Carry


class GoalPublications(Carry):
    """Each goal is republished at the certified wire head it was first seen at."""

    after = 'carry_storage_roots'

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        registry = root / "registry.json"
        history = root / "goal_history.sqlite3"
        with _store_lock(registry):
            wire = WireLog(root / "bus.jsonl")
            with wire.certified_read() as source:
                head = source.committed_sequence()
            store = RegistryStore(registry)
            store.private_guard_unlocked()
            document = json.loads(registry.read_text())
            goals = [thread["goal"] for thread in document["threads"].values()
                     if thread["goal"] is not None]
            connection = sqlite3.connect(f"{history.as_uri()}?mode=rw", uri=True) if history.exists() else None
            with closing(connection) if connection is not None else nullcontext():
                rows = []
                if connection is not None:
                    connection.execute("PRAGMA synchronous=EXTRA")
                    for sequence, before, after in connection.execute(
                        f'SELECT sequence,"before","after" FROM {GoalHistoryEntry.declared_name}'
                    ):
                        pair = [json.loads(value) if value is not None else None for value in (before, after)]
                        rows.append((sequence, pair))
                        goals.extend(goal for goal in pair if goal is not None)
                # A retry after interruption reuses the already-published value for
                # each identity, rather than assigning a second cutover boundary.
                published = {}
                for goal in goals:
                    if "published_seq" in goal:
                        previous = published.setdefault(goal["id"], goal["published_seq"])
                        if previous != goal["published_seq"]:
                            raise ValueError("Conflicting publication sequences for one goal")
                changed = 0
                for goal in goals:
                    if "published_seq" not in goal:
                        goal["published_seq"] = published.setdefault(goal["id"], head)
                        changed += 1
                    FieldCodec.decode(Goal, goal)
                decoded = FieldCodec.decode(RegistryDocument, document)
                if apply and changed:
                    with connection if connection is not None else nullcontext():
                        if connection is not None:
                            for sequence, pair in rows:
                                connection.execute(
                                    f'UPDATE {GoalHistoryEntry.declared_name} SET "before"=?,"after"=? WHERE sequence=?',
                                    (*[json.dumps(goal) if goal is not None else None for goal in pair], sequence),
                                )
                        store.save_unlocked(decoded)
        # The certified read refreshes the wire's metadata and checkpoint, even on a dry run.
        rewritten = (wire.metadata_path.name, private_bus_checkpoint._path(wire.path).name)
        if changed and apply:
            rewritten += (registry.name, store.guard_path.name, *((history.name,) if connection else ()))
        return Carried(changed, {"published_seq": head, "goal_records": len(goals)}, rewritten)


if __name__ == "__main__":
    GoalPublications.main()
