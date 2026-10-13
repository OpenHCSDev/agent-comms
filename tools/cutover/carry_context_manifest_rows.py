"""One-shot manifest-row carry into a separately certified candidate.

Run with the target reader against an immutable, sealed source. This tool never
publishes into the source root. The release owner must keep writers stopped and
install/certify the candidate at its final location before restarting readers.
Rename changes inode revisions: a candidate seal is NOT a live-install seal.
Delete this retired-format reader after the authorized carry and acceptance.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
from typing import Any, Literal

from agent_comms.bus_publication import unique_wire_object
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.private_bus_checkpoint import (
    ContextManifestSources, _SEED, _chain, _directory_sync,
    install_private_bus_checkpoint,
)
from agent_comms.store_files import _atomic_write_text, _store_lock
from agent_comms.turn_context import ContextManifest
from agent_comms.wire_log import WireLog
from agent_comms.wire_record import (
    ContextManifestWireObservation, ObservationWireRecord, WireScan,
)


@dataclass(frozen=True)
class OriginalManifestObservation:
    """The retired row declaration, confined to this one-shot tool."""
    kind: Literal["context_manifest"]
    manifest: ContextManifest


@dataclass(frozen=True)
class OriginalManifestRow:
    observation: OriginalManifestObservation


@dataclass(frozen=True)
class ManifestRowCarry:
    source: Path
    candidate: Path
    root_id: str

    @staticmethod
    def convert(raw: bytes) -> tuple[bytes, ContextManifest | None]:
        if len(raw) > WireScan.max_row_bytes or not raw.endswith(b"\n"):
            raise RelationViolationError("Original row is oversized or incomplete")
        value = FieldCodec.decode(dict[str, Any], json.loads(
            raw, object_pairs_hook=unique_wire_object))
        if "observation" not in value:
            return raw, None
        original = FieldCodec.decode(OriginalManifestRow, value).observation.manifest
        encoded = ObservationWireRecord(ContextManifestWireObservation(original)).to_wire()
        return json.dumps(encoded, allow_nan=False).encode("utf-8") + b"\n", original

    def run(self) -> dict[str, Any]:
        source_root = self.source.absolute()
        if source_root.resolve(strict=True) != source_root:
            raise ValueError("Original root ancestry must not redirect the carry")
        candidate_root = self.candidate.absolute()
        if candidate_root.is_relative_to(source_root) or source_root.is_relative_to(candidate_root):
            raise ValueError("Candidate must be separate from the original root")
        if candidate_root.exists() or candidate_root.is_symlink():
            raise FileExistsError("Candidate must be fresh; do not overwrite retained evidence")
        if candidate_root.parent.resolve(strict=True) != candidate_root.parent:
            raise ValueError("Candidate ancestry must not redirect publication")
        source_log = WireLog(source_root / "bus.jsonl")
        # Do not create even a lock file in a retained source.
        for name in (".bus.jsonl.lock", ".bus_meta.json.lock"):
            if not (source_root / name).is_file():
                raise ValueError("Original canonical lock files are required")
        with _store_lock(source_log.path, shared=True) as lock:
            source = lock.certified_read()
            source.require_current()
            if source.marker.root_id != self.root_id:
                raise ValueError("Original root identity differs from the requested carry")
            source.require_marker(source_log._private_marker_unlocked())
            candidate_root.mkdir(mode=0o700)
            candidate_log = WireLog(candidate_root / "bus.jsonl")
            scan = WireScan(source.marker)
            digest = _SEED
            manifests = rows = 0
            source.stream.seek(0)
            with candidate_log.path.open("xb") as output:
                os.chmod(candidate_log.path, 0o600)
                while raw := source.stream.readline(WireScan.max_row_bytes + 1):
                    converted, original = self.convert(raw)
                    record = scan.read(converted)
                    if original is not None:
                        if record.context_manifests() != (original,):
                            raise ValueError("Carry changed original manifest facts")
                        manifests += 1
                    output.write(converted)
                    digest = _chain(digest, raw)
                    rows += 1
                output.flush()
                os.fsync(output.fileno())
            if digest.hex() != source.witness.digest:
                raise RelationViolationError("Original bytes differ from their sealed certificate")
            if scan.previous_sequence != source.marker.last_seq:
                raise RelationViolationError("Carry changed the committed sequence")
            source.require_current()
            source.require_marker(source_log._private_marker_unlocked())
            _directory_sync(candidate_log.path)
            unsealed = replace(source.marker, checkpoint_version=None, checkpoint_seal=None)
            candidate_log.write_metadata_unlocked(unsealed)
            witness = install_private_bus_checkpoint(candidate_log)
            # Verify the files actually installed, not just the values sent to them.
            with _store_lock(candidate_log.path, shared=True) as candidate_lock:
                installed = candidate_lock.certified_read()
                installed.require_current()
                if replace(installed.marker, checkpoint_version=None, checkpoint_seal=None) != unsealed:
                    raise ValueError("Carry changed original marker facts")
                source.stream.seek(0)
                installed.stream.seek(0)
                candidate_digest = _SEED
                candidate_scan = WireScan(installed.marker)
                for _ in range(rows):
                    before = source.stream.readline(WireScan.max_row_bytes + 1)
                    offset = installed.stream.tell()
                    after = installed.stream.readline(WireScan.max_row_bytes + 1)
                    expected, original = self.convert(before)
                    if after != expected:
                        raise ValueError("Installed row differs from the original carry")
                    record = candidate_scan.read(after)
                    if original is not None:
                        pointer = ContextManifestSources.one(installed.connection, offset=offset)
                        if pointer is None or pointer.length != len(after):
                            raise ValueError("Installed manifest offset/length differs from its row")
                        if pointer.decode_bytes(pointer.read_bytes(installed.stream), self.root_id) != original:
                            raise ValueError("Indexed manifest differs from its original facts")
                        installed.stream.seek(offset + len(after))
                    candidate_digest = _chain(candidate_digest, after)
                if source.stream.read(1) or installed.stream.read(1):
                    raise ValueError("Carry added or removed history rows")
                if candidate_digest.hex() != witness.digest:
                    raise ValueError("Installed certificate differs from the candidate bytes")
                installed.require_current()
                installed.require_marker(candidate_log._private_marker_unlocked())
            source.require_current()
            source.require_marker(source_log._private_marker_unlocked())
            result = {
                "source": str(source_root), "candidate": str(candidate_root),
                "root_id": self.root_id, "rows": rows, "manifests": manifests,
                "source_bytes": source.witness.offset, "candidate_bytes": witness.offset,
                "source_certificate": FieldCodec.encode(source.witness),
                "candidate_certificate": FieldCodec.encode(witness),
                "history_preserved": True, "non_manifest_bytes_preserved": True,
                "manifest_facts_preserved": True, "marker_facts_preserved": True,
                "manifest_offsets_verified": True, "source_unchanged": True,
                "published": False,
            }
            _atomic_write_text(candidate_root / "carry-result.json",
                               json.dumps(result, indent=2) + "\n", fsync_parent=True)
            return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--root-id", required=True)
    arguments = parser.parse_args()
    result = ManifestRowCarry(arguments.source, arguments.candidate, arguments.root_id).run()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
