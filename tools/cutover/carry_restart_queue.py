"""Carry exact JSON restart intents into their atomic row store, then retire JSON.

No source-side reader accepts the retired format. An attempted intent is copied
as attempted, never turned back into pending. Delete this tool after acceptance.
"""

from pathlib import Path
from typing import ClassVar

from agent_comms.child_process import Platform
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.lifecycle_capability import Active, Pending
from agent_comms.locked_store import DurableFile, FileStore, PatternFile, RegistryFile, RootFile, RuntimeFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder
from agent_comms.restart_queue import DIRECTORY, QueuedRestart, RestartQueue
from agent_comms.store_files import MultiFileStore


class OriginalRestartRecord(DurableFile, PatternFile):
    filename: ClassVar[str] = "{id}.json"
    root_directory: ClassVar[str] = DIRECTORY

    @classmethod
    def matching_in(cls, root: Path) -> frozenset[Path]:
        return frozenset(path for path in root.rglob(str(cls.pattern())) if path.is_file())


class OriginalRestartWriter(FileStore, RuntimeFile, MultiFileStore):
    """The outgoing JSON writer's exact lock, retained only for this carry."""

    filename: ClassVar[str] = f"{DIRECTORY}/queue"


class RestartQueueRows(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (OriginalRestartRecord,)

    def originals(self) -> tuple[tuple[Path, QueuedRestart], ...]:
        records = []
        for path in self.input_files():
            record = FieldCodec.decode(QueuedRestart, FieldCodec.parse(path.read_bytes()))
            directory = self.root / DIRECTORY
            locations = (
                directory / "active" / f"{record.owner.lookup}.json",
                directory / "settled" / str(record.owner.lookup) / f"{record.id}.json",
            )
            if path not in locations:
                raise RelationViolationError(f"Restart record has another owner or id: {path}")
            if path == locations[1] and record.state.items_with(Active, record):
                raise RelationViolationError("A settled restart cannot grant a new active attempt")
            records.append((path, record))
        return tuple(records)

    def carry(self, *, apply: bool) -> Carried:
        originals = self.originals()
        if not originals:
            return Carried(0)
        if not apply:
            return Carried(len(originals))
        MaintenanceBarrier(RegistryFile.path_in(self.root)).read().require_held_by(
            MaintenanceHolder.current(), "Carry original restart intents")
        store = RestartQueue(self.root)
        with OriginalRestartWriter.at(self.root).guarded_write(), store.watcher_claim.claimed():
            # Re-read after the outgoing writer is excluded; no intent is inferred from a path.
            originals = self.originals()
            with store.writing() as db:
                for _, record in originals:
                    record.insert_if_absent(db)
                    if QueuedRestart.require(db, id=record.id) != record:
                        raise RelationViolationError("Carried restart differs from its original intent")
        return Carried(len(originals), rewritten=(store.path.relative_to(self.root),))

    def after_acceptance(self) -> Carried:
        originals = self.originals()
        if not originals:
            return Carried(0)
        store = RestartQueue(self.root)
        with OriginalRestartWriter.at(self.root).guarded_write(), store.watcher_claim.claimed():
            originals = self.originals()
            with store.snapshot() as db:
                for _, record in originals:
                    if QueuedRestart.require(db, id=record.id) != record:
                        raise RelationViolationError("Original restart changed before JSON retirement")
            for path, _ in originals:
                path.unlink()
            for parent in {path.parent for path, _ in originals}:
                Platform.current().sync_directory(parent)
        # The current reader waits on maintenance before acting. Target runtimes
        # remain original; an Attempting record is never selected for execution.
        if any(record.state.items_with(Pending, record) for _, record in originals):
            store.start_watcher()
        return Carried(len(originals), rewritten=tuple(
            path.relative_to(self.root) for path, _ in originals))
