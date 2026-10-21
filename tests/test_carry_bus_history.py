"""The migration consumes a bus made by the predecessor's real publisher."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

# This fixture needs the removed writer, not a copy of its implementation.
# The commit is an ancestor of this migration and is retired with the carry test.
PREDECESSOR = "196582f6d"


def publish_predecessor_history(directory: Path) -> None:
    from agent_comms.audience_manifest import FrozenRecipient
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.comms import Comms
    from agent_comms.coordination_tables.publications import PublicationIntents
    from agent_comms.coordination_tables.responses import ResponseObligation
    from agent_comms.historical_views import HistoryArchive
    from agent_comms.messages import InfoKind, Message
    from agent_comms.obligation_states import PublishingResponse
    from agent_comms.read_basis import AllConversation, ConversationCursor, ReadCursor
    from agent_comms.read_ledger import PendingReadNotice, ReadDocument, ReadLedger
    from agent_comms.reason_codes import NoReasonCode
    from agent_comms.recorded_bus import RecordedBusIdentity
    from agent_comms.response_acknowledgement import canonical_publication_key
    from agent_comms.response_conversation import ResponseConversation
    from conftest import publish_initial, responder
    from test_envelope_bus_integration import acquire_message

    directory.mkdir()
    project = directory / "project"
    project.mkdir()
    resource = project / "a.py"
    resource.write_text("original source\n")
    comms = Comms(directory / "wire")
    for name in ("sender", "owner"):
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset({"team"}), worktree=str(project),
            process_identity=ProcessIdentity.capture(os.getpid()),
        ))
    root_id = comms.messaging.initialize_private_initial_protocol(history=(
        Message("sender", "owner", "Retained public history: café", InfoKind, seq=1),
    ))
    question = publish_initial(comms, "sender", "owner", "A real private question")
    acquire_message(comms, "owner", resource)
    original = comms.bus.log.read_delivery_cohort(root_id, question.seq)
    recipient, = original.audience.recipients
    response = Message("owner", "sender", "A real keyed answer", InfoKind,
                       answers=(question.reference,))
    intent = PublicationIntents(
        "migration-proof", response.target, response,
        canonical_publication_key("migration-proof", response.target), response.message_id,
    )
    conversation = ResponseConversation(
        FrozenRecipient(recipient.recipient_lookup, "owner"),
        ResponseObligation("migration-proof", response.target, PublishingResponse(),
                           NoReasonCode(), 0, 0, 1),
        (original,),
    )
    bus = responder(comms.root, comms.registry)
    with bus.publisher.opened_activity() as activity, bus.log.locked():
        bus.publisher._publish_keyed_response_unlocked(
            intent, conversation=conversation, registry_snapshot=comms.registry.snapshot(),
            activity=activity,
        )
    owner = comms.registry.snapshot().require("owner").incarnation.require_recorded()
    document = ReadDocument(
        messages={owner: ReadCursor({AllConversation(): ConversationCursor(1, (3,))})},
        bus_identity=RecordedBusIdentity.capture(comms.bus.log.path),
        transcripts={("owner", "retained-transcript", 42): 17},
        notice=PendingReadNotice("Keep this unread notice"),
    )
    ReadLedger.at(comms.root).replace(document)
    destination = directory / "destination"
    destination.mkdir()
    archived = HistoryArchive(destination / "bus.jsonl").attach(comms.root)
    ReadLedger.at(archived.root).replace(replace(
        document, bus_identity=RecordedBusIdentity.capture(Path(archived.root) / "bus.jsonl")))


def test_predecessor_publisher_history_round_trips_into_bus(tmp_path):
    from cutover.carry_bus_history import (
        BusHistory,
        ClaimHistoryRow,
        HistoryCatalogSource,
        HistoryLine,
        HistoryMetadata,
        HistoryOffset,
    )
    from cutover.carry_bus_read_identity import rebind_read_identity

    from agent_comms.bus import Bus, ClaimRecord, PublicRecord
    from agent_comms.errors import RelationViolationError, UnclaimedError
    from agent_comms.historical_views import HistoryArchive
    from agent_comms.messages import BusRecord
    from agent_comms.read_ledger import ReadLedger
    from agent_comms.recorded_bus import RecordedBusIdentity

    checkout = Path(__file__).resolve().parents[1]
    archive = tmp_path / "publisher.tar"
    with archive.open("wb") as stream:
        subprocess.run(["git", "archive", PREDECESSOR], cwd=checkout, stdout=stream, check=True)
    predecessor = tmp_path / "publisher"
    predecessor.mkdir()
    with tarfile.open(archive) as tree:
        tree.extractall(predecessor, filter="data")
    archive.unlink()
    generated = tmp_path / "generated"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(generated)],
        cwd=predecessor,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            (str(predecessor / "src"), str(predecessor / "tests")))},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archived, = (generated / "destination" / "history").iterdir()
    destination = tmp_path / "carried-destination"
    destination.mkdir()
    shutil.copyfile(HistoryArchive.path_in(generated / "destination"),
                    HistoryArchive.path_in(destination))
    sources = HistoryCatalogSource.read(destination, generated / "destination")
    bindings = {}
    numbers = dict(rows=4, continuous=4, round_tripped=4)
    for original, root in ((generated / "wire", tmp_path / "carried-wire"),
                           (archived, destination / "history" / archived.name)):
        root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(original, root)
        source = root / BusHistory.filename
        source.chmod(0o400)
        original_bytes = source.read_bytes()
        metadata = HistoryMetadata.read(root)
        previous_identity = RecordedBusIdentity.capture(original / BusHistory.filename)
        before = ReadLedger.at(root).read()
        assert BusHistory.carry(root, apply=False).detail == numbers
        assert not Bus.path_in(root).exists()
        with pytest.raises(RelationViolationError, match="spill_dir"):
            BusHistory.carry(root, apply=True)
        assert not Bus.path_in(root).exists()
        bus = Bus(Bus.path_in(root))
        result = BusHistory.transfer(source, bus, metadata)
        assert result.changed == 4
        assert result.detail == numbers
        rows = tuple(HistoryLine.rows(source, metadata))
        claim, = (line for line in rows if isinstance(line.row, ClaimHistoryRow))
        if claim.row.seq > metadata.claimed_after_seq:
            assert bus.claim(claim.row.seq) == claim.row.claim_transition.current()
            assert {type(bus.lookup(line.row.seq)) for line in rows} == set(
                BusRecord.members_with(BusRecord))
        else:
            with pytest.raises(UnclaimedError):
                bus.claim(claim.row.seq)
            assert type(bus.lookup(claim.row.seq)) is PublicRecord
            assert not isinstance(bus.lookup(claim.row.seq), ClaimRecord)
        for line in rows:
            line.verify(bus.lookup(line.row.seq))
        with bus.observing() as db:
            assert HistoryOffset.count(db) == 4
            assert all(HistoryOffset.select(db, offset=line.offset) == [
                HistoryOffset(line.offset, line.row.seq)] for line in rows)
        assert BusHistory.transfer(source, bus, metadata).changed == 0
        assert rebind_read_identity(root, previous_identity).changed == 1
        assert ReadLedger.at(root).read() == replace(
            before, bus_identity=RecordedBusIdentity.capture(bus.path))
        assert rebind_read_identity(root, previous_identity).changed == 0
        bindings[previous_identity] = RecordedBusIdentity.capture(bus.path)
        assert source.read_bytes() == original_bytes
    assert HistoryCatalogSource.publish(
        destination, generated / "destination", sources, bindings).changed == 1
    converted, = HistoryArchive(Bus.path_in(destination)).sources()
    converted.validate()
    assert converted.provenance == sources[0].provenance
    assert converted.bus_identity == bindings[sources[0].bus_identity]
    assert converted.root == str(destination / "history" / archived.name)


if __name__ == "__main__":
    publish_predecessor_history(Path(sys.argv[1]))
