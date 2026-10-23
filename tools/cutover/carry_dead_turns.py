"""Delete the turn files of generations the registry no longer names.

``RegistryEdit.lease_turn`` now deletes the generation it supersedes, but every
bus before that kept each thread's old generations forever (8,688 dead files on
the live bus). A thread's current generation is kept, whether or not a turn still
references it, because a released lease's late transition may still read it. A
thread whose current file is missing has no recorded turn to keep, and the carry
refuses it instead of guessing.

Delete after every bus has run this step.
"""

from __future__ import annotations

from pathlib import Path

from agent_comms.errors import RelationViolationError
from agent_comms.installer.carry import Carried, Carry
from agent_comms.registry_store import RegistryStore
from agent_comms.turn_lease import StoredTurnRecord


class DeadTurns(Carry):
    def backup_files(self) -> tuple[Path, ...]:
        """Every turn file, read without decoding: earlier steps carry the registry first."""
        paths = tuple(sorted(StoredTurnRecord.matching_in(RegistryStore.at(self.root).path.parent)))
        for path in paths:
            self.require_confined(self.root, path)
        return paths

    def carry(self, *, apply: bool) -> Carried:
        registry = RegistryStore.at(self.root)
        if not registry.path.exists():
            return Carried(0)
        current = frozenset(
            StoredTurnRecord.for_identity(registry.path, thread.turn_identity).path
            for thread in registry.read_snapshot_unlocked().threads.values()
            if thread.turn_generation
        )
        missing = sorted(str(path) for path in current if not path.exists())
        if missing:
            raise RelationViolationError(f"Current turn files are missing: {missing}")
        dead = sorted(StoredTurnRecord.matching_in(registry.path.parent) - current)
        for path in dead:
            self.require_confined(self.root, path)
            if apply:
                path.unlink()
        return Carried(len(dead), detail={"kept": len(current), "deleted": len(dead)},
                       rewritten=tuple(path.relative_to(self.root) for path in dead)
                       if apply else ())
