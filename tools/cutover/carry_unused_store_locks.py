"""Delete copy-created locks whose sibling has no locking owner.

The old RootFile.copy_group locked byte files as well as stores. The current
declaring owner answers whether a sibling takes that lock, even before a lazy
store creates its data file. A lock any declaration claims as a member of an
existing primary (a native transcript's writer fence) is live state, whatever its
name parses as; a lock beside a file no declaration recognises is reported, never
deleted. Delete this carry after it has run on the live root.
"""

from pathlib import Path
from typing import ClassVar

from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.store_files import StoreLockFile, is_store_lock, locked_store_path, store_lock_path


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

    def claimed(self) -> frozenset[Path]:
        """Every path a declaration owns beside a primary on disk, except the generic store
        lock of a primary whose declaring owner takes no store lock: that one is the copy's."""
        return frozenset(
            member
            for path, owner in RootFile.primaries_in(self.root).items()
            for member in owner.member_paths(path) | owner.transient_paths(path)
            if member != store_lock_path(path)
            or issubclass(owner.declaring_owner(), StoreLockFile)
        )

    def carry(self, *, apply: bool) -> Carried:
        locks = self.input_files()
        claimed = self.claimed()
        siblings = {locked_store_path(lock): lock for lock in locks if lock not in claimed}
        resources = {
            self.root / resource.path: resource
            for resource in RootFile.owned_files(self.root, tuple(siblings))
        }
        declared = {sibling: resources[sibling] for sibling in siblings
                    if sibling in resources and resources[sibling].declared}
        unused = tuple(siblings[sibling] for sibling, resource in declared.items()
                       if not resource.takes_store_lock())
        if apply:
            for path in unused:
                path.unlink()
        return Carried(
            len(unused),
            detail={
                "deleted": len(unused),
                "kept": len(locks) - len(siblings) + len(declared) - len(unused),
                "unknown": len(siblings) - len(declared),
            },
        )
