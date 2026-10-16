"""Explicit producer declaration for the existing acquired owner read.

The release owner selects the authentic producer runtime and its format. Neither
member discovers formats from missing fields or offers a fallback reader.
"""
from abc import abstractmethod
from pathlib import Path
from typing import get_type_hints

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.registry_document import RegistryDocument
from agent_comms.owner_lifecycle import OwnerReleaseStore, OwnerRestartSelection
from agent_comms.private_path import FileRevision
from agent_comms.wire_log import WireLog


class OwnerReadProjection(DeclaredFamily, affix='Projection'):
    @classmethod
    def require_source_runtime(cls) -> None:
        """Current/recorded reads use their runtime's normal strict declaration."""

    @classmethod
    @abstractmethod
    def read(cls, registry, root: Path, name: str, request):
        """Acquire evidence through its original declaration owner."""


class LiveOwnerReadProjection(OwnerReadProjection):
    @classmethod
    def read(cls, registry, root: Path, name: str, request):
        """Acquire the original live selection before projecting its document."""
        with registry.store.reading() as document:
            snapshot = document.snapshot()
            selection = (
                FieldCodec.decode(OwnerRestartSelection, request)
                if request is not None else OwnerRestartSelection.capture(snapshot, name)
            )
            selection.require_current(snapshot)
            return dict(cls.project(root, registry.store.path, document),
                        selection=FieldCodec.encode(selection))

    @classmethod
    @abstractmethod
    def project(cls, root: Path, registry_path: Path, document: RegistryDocument) -> dict:
        """Project a document already validated by its authentic producer."""


class RetiredGoalReportProjection(LiveOwnerReadProjection):
    @classmethod
    def require_source_runtime(cls) -> None:
        from thread_format_retirement import GoalReportMemberRetirement

        GoalReportMemberRetirement.require_original_declaration()

    @classmethod
    def project(cls, root: Path, registry_path: Path, document: RegistryDocument) -> dict:
        from thread_format_retirement import GoalReportMemberRetirement

        releases = OwnerReleaseStore(root / 'owner_release_receipts.json')
        with releases.locked():
            acquired = GoalReportMemberRetirement.acquire(
                registry_path, document, releases._read_unlocked())
            projected = acquired.project()
        return {'document': projected['registry'], 'releases': projected['releases'],
                'goal_history': projected['goal_history']}


class CurrentThreadProjection(LiveOwnerReadProjection):
    @classmethod
    def project(cls, root: Path, registry_path: Path, document: RegistryDocument) -> dict:
        return {'document': FieldCodec.encode(document)}


class RecordedRegistryProjection(OwnerReadProjection):
    """Original typed values projected by the target's recorded declaration."""

    @classmethod
    def project(cls, document: RegistryDocument, declaration) -> dict:
        requested = declaration['properties']['threads']['additionalProperties']['properties']
        declarations = get_type_hints(type(document), include_extras=True)
        encoded = FieldCodec.encode(document.threads, declarations['threads'])
        return {
            'threads': {name: {key: thread[key] for key in requested}
                        for name, thread in encoded.items()},
            'aliases': FieldCodec.encode(document.aliases, declarations['aliases']),
        }

    @classmethod
    def read(cls, registry, root: Path, name: str, request):
        """Recorded evidence has no live selection/process admission."""
        with registry.store.reading() as document:
            return cls.capture(root, registry.store.path, document, request)

    @classmethod
    def capture(cls, root: Path, registry_path: Path, document, declaration):
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
        return {'root': str(root), 'original_root': str(root),
                'wire_root_id': marker.root_id, 'bus_identity': [info.st_dev, info.st_ino],
                'size': info.st_size, 'snapshot_bus_revision': FieldCodec.encode(bus_revision),
                'snapshot_registry_revision': FieldCodec.encode(registry_revision), 'provenance': provenance}
