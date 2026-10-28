"""Publish each claim's saved re-arm record beside its lock, where the current watch reads it."""

import json
from pathlib import Path
from typing import ClassVar

from agent_comms.cli_bridges.shared import WatchRecordStore
from agent_comms.errors import RelationViolationError
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.store_files import ExclusiveAtomicFile
from agent_comms.typed_table import WriterOwnedResource
from agent_comms_hooks.watch import WatchClaim, WatchRecord


class WatchRecordSiblings(Carry):
    """An older watch saved its record inside the claim lock. The current watch reads only the
    sibling record, so a lapsed session of this bus would lose its re-arm without this carry.
    Lock bytes and inodes are never written: a live holder's flock lives on that inode."""

    opened_files: ClassVar[tuple[type[RootFile], ...]] = ()
    writer_owned = (WatchRecordStore,)

    @staticmethod
    def saved_root(path: Path, saved: bytes) -> str:
        """The bus root a saved record names; older formats of other buses stay theirs."""
        try:
            fields = json.loads(saved)
        except ValueError as error:
            raise RelationViolationError(f"{path}: unreadable saved watch record: {error}") from error
        root = fields.get("root") if isinstance(fields, dict) else None
        if not isinstance(root, str):
            raise RelationViolationError(f"{path}: saved watch record names no bus root")
        return root

    @classmethod
    def decoded(cls, path: Path, saved: bytes) -> WatchRecord:
        cls.saved_root(path, saved)
        try:
            return WatchRecord.decode(**json.loads(saved))
        except TypeError as error:
            raise RelationViolationError(f"{path}: unreadable saved watch record: {error}") from error

    def carry(self, *, apply: bool) -> Carried:
        root = str(self.files.source_root())
        pending: list[tuple[WriterOwnedResource, WatchRecord]] = []
        current = empty = foreign = 0
        # Every candidate decodes before anything is written, so a malformed one refuses cleanly.
        for resource in WatchRecordStore.resources(self.files):
            claim = self.files.location(resource)
            sibling = WatchClaim.record_beside(claim)
            self.require_confined(claim)
            self.require_confined(sibling)
            if sibling.exists():
                self.decoded(sibling, sibling.read_bytes())
                current += 1
                continue
            saved = claim.read_bytes()
            if not saved.strip():
                empty += 1
                continue
            if self.saved_root(claim, saved) != root:
                foreign += 1
                continue
            pending.append((resource, self.decoded(claim, saved)))
        published: list[WriterOwnedResource] = []
        if apply:
            for resource, record in pending:
                sibling = WatchClaim.record_beside(self.files.location(resource))
                try:
                    ExclusiveAtomicFile(sibling).write_text(record.to_json())
                except FileExistsError:
                    current += 1  # a current session published its own record meanwhile
                    continue
                if self.decoded(sibling, sibling.read_bytes()) != record:
                    raise RelationViolationError(f"{sibling}: published record does not decode back")
                published.append(resource)
        return Carried(len(published) if apply else len(pending),
                       detail={"current": current, "empty": empty, "other_bus": foreign},
                       rewritten=tuple(resource.path for resource in published))
