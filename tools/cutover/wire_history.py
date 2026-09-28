"""Stage D22 current wire history without changing a source; delete after activation."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from agent_comms.bus_publication import PRIVATE_WIRE_FIELD, unique_wire_object
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message
from agent_comms.store_files import file_revision
from agent_comms.wire_log import WireLog
from agent_comms.wire_metadata import ArchivedAccess, WireAccess, WireMetadata, WritableAccess


@dataclass(frozen=True)
class WireRewrite:
    source: str
    staged: str
    root_id: str
    messages: int
    history_only: int
    admission_after_seq: int


def stage(source: Path, destination: Path, access: WireAccess) -> WireRewrite:
    """Canonicalize every row, preserving public fields, order and private evidence.

    The output is an isolated reviewable candidate. Installation is a separate
    quiet operation which must replace the source in place, reset derived stores
    and refresh attached-history revisions. This function never activates it.
    """
    source = source.absolute()
    destination = destination.absolute()
    if destination == source or destination.is_relative_to(source):
        raise ValueError("Stage must be separate from retained source history")
    wire = source / "bus.jsonl"
    marker_path = source / "bus_meta.json"
    if not marker_path.exists():
        marker_path = source / "source_bus_meta.json"
    before = (file_revision(wire), file_revision(marker_path))
    marker = (
        json.loads(marker_path.read_text(), object_pairs_hook=unique_wire_object)
        if marker_path.exists()
        else {}
    )
    stored = FieldCodec.decode(
        WireMetadata,
        {
            "admission_after_seq": 0,
            "access": FieldCodec.encode(WritableAccess()),
            **marker,
        },
    )
    root_id = stored.wire_root_id or uuid.uuid4().hex
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    count = historical = last_seq = 0
    with wire.open("rb") as original, (destination / "bus.jsonl").open("xb") as output:
        os.chmod(output.name, 0o600)
        for raw in original:
            if not raw.endswith(b"\n"):
                raise ValueError("Incomplete source row; preserve it for explicit reconciliation")
            record = json.loads(raw, object_pairs_hook=unique_wire_object)
            message = Message.from_wire(record)
            if message.seq <= last_seq:
                raise ValueError("Source sequences are not strictly increasing")
            canonical = message.to_wire()
            public = {key: value for key, value in record.items() if key != PRIVATE_WIRE_FIELD}
            if canonical != public:
                raise ValueError("Source payload needs an explicit declared conversion")
            if PRIVATE_WIRE_FIELD in record:
                canonical[PRIVATE_WIRE_FIELD] = record[PRIVATE_WIRE_FIELD]
            elif message.claim_transition is None:
                historical += 1
            output.write(
                json.dumps(canonical, ensure_ascii=False, allow_nan=False).encode() + b"\n"
            )
            count += 1
            last_seq = message.seq
        output.flush()
        os.fsync(output.fileno())
    highwater = max(last_seq, stored.last_seq)
    current = WireMetadata(
        access=access,
        last_seq=highwater,
        admission_after_seq=highwater,
        writer_protocol_version=1,
        wire_root_id=root_id,
        claim_envelopes_version=1,
    )
    log = WireLog(destination / "bus.jsonl")
    log.write_metadata_unlocked(current)
    # Validate complete current protocol, including original initial/response
    # bindings. No historical audience, decision, claim or native proof is made.
    with log.locked():
        verified = sum(1 for _ in log._verified_private_rows_unlocked(current))
    if verified != count or before != (file_revision(wire), file_revision(marker_path)):
        raise ValueError("Source changed during staging; discard this candidate")
    result = WireRewrite(str(source), str(destination), root_id, count, historical, highwater)
    (destination / "rewrite-receipt.json").write_text(
        json.dumps(FieldCodec.encode(result), indent=2) + "\n"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--archive", action="store_true")
    arguments = parser.parse_args()
    access = ArchivedAccess() if arguments.archive else WritableAccess()
    print(
        json.dumps(
            FieldCodec.encode(stage(arguments.source, arguments.destination, access)), indent=2
        )
    )
