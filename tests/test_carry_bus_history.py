"""The migration consumes a bus made by the predecessor's real publisher."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
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
    from agent_comms.messages import InfoKind, Message
    from agent_comms.obligation_states import PublishingResponse
    from agent_comms.reason_codes import NoReasonCode
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


def test_predecessor_publisher_history_round_trips_into_bus(tmp_path):
    from cutover.carry_bus_history import BusHistory, HistoryLine, HistoryOffset

    from agent_comms.bus import Bus
    from agent_comms.errors import RelationViolationError
    from agent_comms.messages import BusRecord

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
    root = tmp_path / "carried"
    root.mkdir()
    source = root / BusHistory.filename
    shutil.copyfile(generated / "wire" / BusHistory.filename, source)
    source.chmod(0o400)
    original_bytes = source.read_bytes()
    numbers = dict(rows=4, continuous=4, round_tripped=4)
    assert BusHistory.carry(root, apply=False).detail == numbers
    assert not Bus.path_in(root).exists()
    # Installer staging is an explicit missing integration, never a pretend success.
    with pytest.raises(RelationViolationError, match="spill_dir"):
        BusHistory.carry(root, apply=True)
    assert not Bus.path_in(root).exists()
    bus = Bus(Bus.path_in(root))
    result = BusHistory.transfer(source, bus)
    assert result.changed == 4
    assert result.detail == numbers
    rows = tuple(HistoryLine.rows(source))
    assert {type(bus.lookup(line.row.seq)) for line in rows} == set(
        BusRecord.members_with(BusRecord))
    assert all(line.row.restored(bus.lookup(line.row.seq)).line() == line.raw for line in rows)
    with bus.observing() as db:
        assert HistoryOffset.count(db) == 4
        assert all(HistoryOffset.select(db, offset=line.offset) == [
            HistoryOffset(line.offset, line.row.seq)] for line in rows)
    assert BusHistory.transfer(source, bus).changed == 0
    assert source.read_bytes() == original_bytes


if __name__ == "__main__":
    publish_predecessor_history(Path(sys.argv[1]))
