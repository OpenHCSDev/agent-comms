"""Current recorded-source acquisition, separate from the retained P operation."""
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from agent_comms.field_codec import FieldCodec
from agent_comms.private_path import FileRevision
from agent_comms.registry_document import RegistryDocument
from agent_comms.wire_log import WireLog


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
    provenance: dict[str, JsonValue]

    @classmethod
    def project(cls, document: RegistryDocument, declaration) -> dict[str, JsonValue]:
        requested = declaration['properties']['threads']['additionalProperties']['properties']
        # The original registry owns both container grammars. Encoding it retains
        # its actual source annotations, including source-only Thread members.
        encoded = FieldCodec.encode(document)
        return {
            'threads': {name: {key: thread[key] for key in requested}
                        for name, thread in encoded['threads'].items()},
            'aliases': encoded['aliases'],
        }

    @classmethod
    def capture(cls, root: Path, registry_path: Path, document: RegistryDocument,
                declaration):
        """Caller owns the original read/writer throughout proof acquisition."""
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
        return cls(str(root), str(root), marker.root_id, (info.st_dev, info.st_ino),
                   info.st_size, bus_revision, registry_revision, provenance)
