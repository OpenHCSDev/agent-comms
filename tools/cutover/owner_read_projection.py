"""Explicit producer declaration for the existing acquired owner read.

The release owner selects the authentic producer runtime and its format. Neither
member discovers formats from missing fields or offers a fallback reader.
"""
from abc import abstractmethod
from pathlib import Path

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.registry_document import RegistryDocument
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.store_files import file_revision
from agent_comms.wire_log import WireLog
from thread_format_retirement import GoalReportMemberRetirement


class OwnerReadProjection(DeclaredFamily, affix='Projection'):
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
            return {'document': cls.project(document),
                    'selection': FieldCodec.encode(selection)}

    @classmethod
    @abstractmethod
    def project(cls, document: RegistryDocument) -> dict:
        """Project a document already validated by its authentic producer."""


class RetiredGoalReportProjection(LiveOwnerReadProjection):
    @classmethod
    def project(cls, document: RegistryDocument) -> dict:
        return GoalReportMemberRetirement.threads(FieldCodec.encode(document))


class CurrentThreadProjection(LiveOwnerReadProjection):
    @classmethod
    def project(cls, document: RegistryDocument) -> dict:
        return FieldCodec.encode(document)


class RecordedRegistryProjection(OwnerReadProjection):
    """Original typed values projected by the target's recorded declaration."""

    @classmethod
    def project(cls, document: RegistryDocument, declaration) -> dict:
        requested = declaration['properties']['threads']['additionalProperties']['properties']
        threads = {}
        for name, thread in document.threads.items():
            declared = {key: item for item, key in FieldCodec._fields(type(thread))}
            threads[name] = {
                key: FieldCodec.encode(getattr(thread, declared[key].name)) for key in requested
            }
        return {'threads': threads, 'aliases': FieldCodec.encode(document.aliases)}

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
        bus_revision = file_revision(log.path)
        registry_revision = file_revision(registry_path)
        marker = log.read_metadata_unlocked(required=True)
        provenance = cls.project(document, declaration)
        info = log.path.stat()
        if (bus_revision != file_revision(log.path)
                or registry_revision != file_revision(registry_path)):
            raise ValueError('Original recorded source changed during acquisition')
        return {'root': str(root), 'original_root': str(root),
                'wire_root_id': marker.root_id, 'bus_identity': [info.st_dev, info.st_ino],
                'size': info.st_size, 'snapshot_bus_revision': bus_revision,
                'snapshot_registry_revision': registry_revision, 'provenance': provenance}
