"""The migration consumes a bus made by the predecessor's real publisher."""
from __future__ import annotations

import json
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
    shutil.copytree(comms.root, destination)
    ReadLedger.at(destination).replace(replace(
        document, bus_identity=RecordedBusIdentity.capture(destination / "bus.jsonl")))
    archived = HistoryArchive(destination / "bus.jsonl").attach(comms.root)
    ReadLedger.at(archived.root).replace(replace(
        document, bus_identity=RecordedBusIdentity.capture(Path(archived.root) / "bus.jsonl")))


def test_predecessor_publisher_history_round_trips_into_bus(tmp_path):
    from cutover.carry_bus_history import (
        BusHistory,
        ClaimHistoryRow,
        HistoryLine,
        HistoryMetadata,
        HistoryOffset,
    )

    from agent_comms.bus import Bus, ClaimRecord, PublicRecord
    from agent_comms.core_stamp import WheelStamp
    from agent_comms.errors import UnclaimedError
    from agent_comms.field_codec import FieldCodec
    from agent_comms.historical_views import HistoryArchive
    from agent_comms.installer.carry import Carried
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
    commit = subprocess.check_output(
        ["git", "rev-parse", PREDECESSOR], cwd=checkout, text=True).strip()
    WheelStamp(commit).write(predecessor / "src" / "agent_comms" / "package_stamp.py")
    generated = tmp_path / "generated"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(generated)],
        cwd=predecessor,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            (str(predecessor / "src"), str(predecessor / "tests")))},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    original = generated / "destination"
    archived, = (original / "history").iterdir()
    root = tmp_path / "carried"
    spill = tmp_path / "hdd-spill"
    spill.mkdir()
    shutil.copytree(original, root)
    relatives = (Path(), archived.relative_to(original))
    originals = {relative: (original / relative / BusHistory.filename).read_bytes()
                 for relative in relatives}
    before = {relative: ReadLedger.at(root / relative).read() for relative in relatives}
    dry = BusHistory(root, original, spill, False)
    assert dry.carry().detail == dict(rows=8, continuous=8, round_tripped=8)
    assert all(not Bus.path_in(root / relative).exists() for relative in relatives)
    assert not tuple(spill.iterdir())
    request = replace(dry, apply=True)
    completed = subprocess.run(
        [sys.executable, "-m", "agent_comms.installer.step_worker", request.name(), "carry",
         json.dumps(FieldCodec.encode(request))], cwd=checkout,
        capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = FieldCodec.decode(Carried, FieldCodec.parse(completed.stdout.splitlines()[-1]))
    assert result.changed == 8
    assert {key: result.detail[key] for key in ("rows", "continuous", "round_tripped")} == {
        "rows": 8, "continuous": 8, "round_tripped": 8,
    }
    assert result.detail["read_viewers_before"] == result.detail["read_viewers_after"] == 2
    assert (result.detail["read_conversations_before"]
            == result.detail["read_conversations_after"] == 2)
    assert result.detail["read_transcripts_before"] == result.detail["read_transcripts_after"] == 2
    staged, = spill.iterdir()
    for relative in relatives:
        directory = root / relative
        source = staged / relative / BusHistory.filename
        metadata = HistoryMetadata.read(source.parent)
        assert not (directory / BusHistory.filename).exists()
        assert not (directory / HistoryMetadata.filename).exists()
        assert source.stat().st_mode & 0o777 == 0o400
        assert source.read_bytes() == originals[relative]
        assert (original / relative / BusHistory.filename).read_bytes() == originals[relative]
        bus = Bus(Bus.path_in(directory))
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
        assert ReadLedger.at(directory).read() == replace(
            before[relative], bus_identity=RecordedBusIdentity.capture(bus.path))
        assert BusHistory.rebind_reads(directory, before[relative].bus_identity).changed == 0
    converted, = HistoryArchive(Bus.path_in(root)).sources()
    converted.validate()
    assert converted.root == str(root / archived.relative_to(original))


if __name__ == "__main__":
    publish_predecessor_history(Path(sys.argv[1]))
