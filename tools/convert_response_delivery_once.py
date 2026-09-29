"""One-time, offline conversion; removed from the shipping tree after rehearsal.

Run with the NEW installed interpreter and --old-python pointing to the previous
immutable runtime. Stop writers first. Public messages, sequences, root, floor,
SQL execution state and UNKNOWN barriers are preserved. No prompt is sent.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from agent_comms.audience_manifest import FrozenRecipient
from agent_comms.bus_publication import (
    PRIVATE_WIRE_FIELD,
    initial_sideband,
    validate_delivery_record,
)
from agent_comms.checkpoint_seals import FinalSeal, file_revision
from agent_comms.coordinator import Coordination
from agent_comms.delivery_policy import ResponseDeliveryPolicy
from agent_comms.messages import Message
from agent_comms.private_bus_checkpoint import (
    PrefixCertificate,
    _connect,
    _saved,
    install_private_bus_checkpoint,
)
from agent_comms.response_conversation import ResponseConversation
from agent_comms.store_files import _store_lock
from agent_comms.wake import ControlClassification
from agent_comms.wire_log import WireLog

OLD_VERIFY = """
from pathlib import Path
import sys
from agent_comms.wire_log import WireLog
from agent_comms.private_bus_checkpoint import verify_private_bus_checkpoint_unlocked
bus=WireLog(Path(sys.argv[1])/'bus.jsonl')
marker=bus._private_marker_unlocked()
verify_private_bus_checkpoint_unlocked(bus,marker)
list(bus._verified_private_rows_unlocked(marker))
"""


def sync(path):
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def convert(root: Path, old_python: Path):
    bus = WireLog(root / "bus.jsonl")
    with _store_lock(root / "wire"), bus.locked():
        subprocess.run([str(old_python), "-c", OLD_VERIFY, str(root)], check=True)
        marker = bus._private_marker_unlocked()
        backup = root / "response-delivery-before"
        backup.mkdir(mode=0o700)  # Refuse repeated/unreviewed cutover.
        paths = [bus.path, root / "private_bus_checkpoint.sqlite3", bus.metadata_path]
        for path in paths:
            shutil.copy2(path, backup / path.name)
            sync(backup / path.name)
        committed = {}
        rewritten = []
        converted = []
        with Coordination(str(root / "coordination.sqlite3")) as store:
            for raw in bus.path.read_bytes().splitlines(keepends=True):
                record = json.loads(raw)
                private = record.get(PRIVATE_WIRE_FIELD)
                if private is None:
                    rewritten.append(raw)
                    continue
                message = Message.from_wire(record)
                if "response" in private:
                    snapshot = store.snapshots.get(private["response"]["execution_id"])
                    sources = tuple(committed[a.wire_seq] for a in snapshot.assignments)
                    conversation = ResponseConversation(
                        FrozenRecipient(
                            snapshot.execution.owner_lookup, snapshot.execution.owner_thread
                        ),
                        snapshot.execution.exact_target,
                        sources,
                    )
                    audience = conversation.audience(message)
                    decisions = ResponseDeliveryPolicy().resolve(
                        message, audience, ControlClassification.ORDINARY
                    )
                    private["initial"] = initial_sideband(
                        marker.root_id, message, audience, decisions, control="ordinary"
                    )
                    raw = json.dumps(record, allow_nan=False).encode() + b"\n"
                    converted.append(message.seq)
                delivery = validate_delivery_record(record, marker.root_id)
                committed[message.seq] = delivery
                rewritten.append(raw)
            # Reuse existing participant owner; never invent current membership.
            for source in committed.values():
                if source.message.sender_role.executable:
                    audience = source.audience
                    store.participants.register(
                        audience.sender_lookup,
                        audience.sender_name,
                        audience.sender_name,
                        committed=True,
                    )
        with tempfile.TemporaryDirectory(prefix=".response-delivery-", dir=root) as directory:
            stage = Path(directory)
            staged_bus = WireLog(stage / "bus.jsonl")
            staged_bus.path.write_bytes(b"".join(rewritten))
            staged_bus.path.chmod(0o600)
            sync(staged_bus.path)
            marker.checkpoint_version = None
            marker.checkpoint_seal = None
            staged_bus.write_metadata_unlocked(marker)
            install_private_bus_checkpoint(staged_bus)
            # Stage certified inode-bound artifacts on the same filesystem.
            # Until marker publishes last, old seal mismatch fails closed.
            for path in paths[:-1]:
                os.replace(stage / path.name, path)
            # Rename changes ctime. Seal final in-place inode revisions, using
            # the already verified staged digest and unchanged bytes.
            checkpoint = root / "private_bus_checkpoint.sqlite3"
            with closing(_connect(checkpoint)) as db:
                saved = _saved(db)
                info = bus.path.stat()
                witness = replace(saved, revision=file_revision(info))
                with db:
                    PrefixCertificate.capture(
                        marker.root_id,
                        info,
                        saved.through_seq,
                        bytes.fromhex(saved.digest),
                        saved.tail,
                    ).upsert(db)
            marker.seal_with(FinalSeal.capture(witness, checkpoint))
            bus.write_metadata_unlocked(marker)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        actual = list(bus._verified_private_rows_unlocked(bus._private_marker_unlocked()))
        assert [m.to_wire() for m, _, _ in actual] == [
            Message.from_wire(json.loads(raw)).to_wire()
            for raw in (backup / "bus.jsonl").read_bytes().splitlines()
        ]
        return {
            "converted_response_seqs": converted,
            "messages": len(actual),
            "backup": str(backup),
            "root_id": marker.root_id,
            "admission_after_seq": marker.admission_after_seq,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--old-python", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(convert(args.root, args.old_python), indent=2))
