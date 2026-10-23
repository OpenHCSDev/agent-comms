"""Delete copy-created locks whose sibling has no locking owner.

The old RootFile.copy_group locked byte files as well as stores. The current
owner class answers whether a sibling takes that lock; absent siblings cannot
use it. Delete this carry after it has run on the live root.
"""

from pathlib import Path
from typing import ClassVar

from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.store_files import StoreLockOwner, is_store_lock, locked_store_path


class UnusedStoreLocks(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = ()
    """Only lock artifacts are inputs; sibling content is never opened."""

    def input_files(self) -> tuple[Path, ...]:
        locks = tuple(
            sorted(path for path in self.root.rglob("*") if is_store_lock(path) and path.is_file())
        )
        for path in locks:
            self.require_confined(self.root, path)
        return locks

    def carry(self, *, apply: bool) -> Carried:
        locks = self.input_files()
        siblings = tuple(locked_store_path(path) for path in locks)
        locking = {
            self.root / resource.path
            for resource, owner in RootFile.owned_files(self.root, siblings).items()
            if issubclass(owner, StoreLockOwner)
        }
        unused = tuple(
            lock for lock, sibling in zip(locks, siblings, strict=True) if sibling not in locking
        )
        if apply:
            for path in unused:
                path.unlink()
        return Carried(
            len(unused),
            detail={"deleted": len(unused), "kept": len(locks) - len(unused)},
            rewritten=tuple(path.relative_to(self.root) for path in unused) if apply else (),
        )
