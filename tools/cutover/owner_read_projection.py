"""Explicit producer declaration for the existing acquired owner read.

The release owner selects the authentic producer runtime and its format. Neither
member discovers formats from missing fields or offers a fallback reader.
"""
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.registry_document import RegistryDocument
from agent_comms.goal_history import GoalHistoryEntry
from agent_comms.owner_lifecycle import OwnerReleaseReceipt, OwnerReleaseStore, OwnerRestartSelection
from agent_comms.private_path import FileRevision
from agent_comms.wire_log import WireLog


# This declaration is only for JSON already produced by the source codec, not
# permission to put undeclared domain values in an output packet.
EncodedRecord = dict[str, Any]


@dataclass(frozen=True)
class OriginalOwnerRead:
    """Current source values retain their declarations through stdio and decoding."""

    document: RegistryDocument
    selection: OwnerRestartSelection
    releases: dict[str, OwnerReleaseReceipt] = field(default_factory=dict)
    goal_history: tuple[GoalHistoryEntry, ...] = ()


@dataclass(frozen=True)
class RetiredOwnerRead:
    """Only codec-produced target postimages are opaque JSON, never live values."""

    document: EncodedRecord
    selection: OwnerRestartSelection
    releases: dict[str, EncodedRecord]
    goal_history: tuple[GoalHistoryEntry, ...]


@dataclass(frozen=True)
class RecordedOwnerRead:
    """Producer proofs stay typed; provenance is the requested encoded postimage."""

    root: str
    original_root: str
    wire_root_id: str
    bus_identity: tuple[int, int]
    size: int
    snapshot_bus_revision: FileRevision
    snapshot_registry_revision: FileRevision
    provenance: EncodedRecord


class OwnerReadProjection(DeclaredFamily, affix='Projection'):
    @classmethod
    def require_source_runtime(cls) -> None:
        """Current/recorded reads use their runtime's normal strict declaration."""

    @classmethod
    @abstractmethod
    def read(cls, registry, root: Path, name: str, request) -> OriginalOwnerRead | RetiredOwnerRead | RecordedOwnerRead:
        """Acquire evidence through its original declaration owner."""


class LiveOwnerReadProjection(OwnerReadProjection):
    @classmethod
    def read(cls, registry, root: Path, name: str, request) -> OriginalOwnerRead | RetiredOwnerRead:
        """Acquire the original live selection before projecting its document."""
        with registry.store.reading() as document:
            snapshot = document.snapshot()
            selection = (
                FieldCodec.decode(OwnerRestartSelection, request)
                if request is not None else OwnerRestartSelection.capture(snapshot, name)
            )
            selection.require_current(snapshot)
            return cls.project(root, registry.store.path, document, selection)

    @classmethod
    @abstractmethod
    def project(cls, root: Path, registry_path: Path, document: RegistryDocument,
                selection: OwnerRestartSelection) -> OriginalOwnerRead | RetiredOwnerRead:
        """Project an acquired selection without discarding its declarations."""


class RetiredGoalReportProjection(LiveOwnerReadProjection):
    @classmethod
    def require_source_runtime(cls) -> None:
        from thread_format_retirement import GoalReportMemberRetirement

        GoalReportMemberRetirement.require_original_declaration()

    @classmethod
    def project(cls, root: Path, registry_path: Path, document: RegistryDocument,
                selection: OwnerRestartSelection) -> RetiredOwnerRead:
        from thread_format_retirement import GoalReportMemberRetirement

        releases = OwnerReleaseStore(root / 'owner_release_receipts.json')
        with releases.locked():
            acquired = GoalReportMemberRetirement.acquire(
                registry_path, document, releases._read_unlocked())
            projected = acquired.project()
        return RetiredOwnerRead(projected['registry'], selection,
                                projected['releases'], acquired.history)


class CurrentThreadProjection(LiveOwnerReadProjection):
    @classmethod
    def project(cls, root: Path, registry_path: Path, document: RegistryDocument,
                selection: OwnerRestartSelection) -> OriginalOwnerRead:
        return OriginalOwnerRead(document, selection)


class RecordedRegistryProjection(OwnerReadProjection):
    """Original typed values projected by the target's recorded declaration."""

    @classmethod
    def project(cls, document: RegistryDocument, declaration) -> EncodedRecord:
        requested = declaration['properties']['threads']['additionalProperties']['properties']
        encoded = FieldCodec.encode(document)
        return {
            'threads': {name: {key: thread[key] for key in requested}
                        for name, thread in encoded['threads'].items()},
            'aliases': encoded['aliases'],
        }

    @classmethod
    def read(cls, registry, root: Path, name: str, request) -> RecordedOwnerRead:
        """Recorded evidence has no live selection/process admission."""
        with registry.store.reading() as document:
            return cls.capture(root, registry.store.path, document, request)

    @classmethod
    def capture(cls, root: Path, registry_path: Path, document, declaration) -> RecordedOwnerRead:
        """Project while the original read/writer owns the decoded document."""
        root = root.resolve()
        log = WireLog(root / 'bus.jsonl')
        bus_revision = FileRevision.capture(log.path)
        registry_revision = FileRevision.capture(registry_path)
        marker = log.read_metadata_unlocked(required=True)
        provenance = cls.project(document, declaration)
        info = log.path.stat()
        if (
            bus_revision != FileRevision.capture(log.path)
            or registry_revision != FileRevision.capture(registry_path)
        ):
            raise ValueError('Original recorded source changed during acquisition')
        return RecordedOwnerRead(
            str(root), str(root), marker.root_id, (info.st_dev, info.st_ino),
            info.st_size, bus_revision, registry_revision, provenance,
        )
