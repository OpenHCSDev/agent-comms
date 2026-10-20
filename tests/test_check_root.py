"""Selected check history is small, non-executable and scoped to the command."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys

import pytest

from agent_comms.check_root import CheckRoot
from agent_comms.cli import main
from agent_comms.comms import Comms
from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.envelope_claim_transitions import (
    ClaimRelease, NativeClaim, ReleasedClaims, WakeClaims,
)
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.transcript_events import AssistantTranscript, NoticeTranscript, UserTranscript
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_entries import NativeEntry
from agent_comms.messages import ClaimMessage
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.transcript_events import AssistantTranscript
from agent_comms.turn_digests import LastTurns, TurnDigestRead


def test_only_selected_registry_and_history_without_old_pending_input(source, tmp_path):
    root = tmp_path / "selected"
    with CheckRoot(source, ("A", "B"), root).open() as c:
        assert set(c.registry.snapshot().threads) == {"A", "B"}
        assert "other" not in c.registry.snapshot().aliases
        assert not c.bus.inbox("B")
        assert all(not t.process_alive and not t.executing for t in c.registry.snapshot().threads.values())
        with c.bus.log.opened_wire_snapshot() as history:
            assert [m.body for m in history.newest_messages()] == ["old history is not a new input"]
        assert sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) < 2 * 1024 * 1024
    assert not root.exists()


@pytest.mark.parametrize("timestamp", (None, 1_760_000_000.125))
def test_external_projection_preserves_event_clocks_or_refuses_unknown(source, tmp_path, timestamp):
    owner = source.registry.require("B")
    external = ExternalTranscript(source.root, owner.incarnation)
    events = (
        UserTranscript("source request " * 1500, position=1, timestamp=timestamp),
        AssistantTranscript("source reply", position=2,
                            timestamp=None if timestamp is None else timestamp + 1.125),
        NoticeTranscript("not portable conversation", position=3, timestamp=timestamp),
        UserTranscript("", position=4, timestamp=timestamp),
    )
    root = tmp_path / "external-window"
    if timestamp is None:
        # Current records require a clock at their storage boundary, before copying.
        with pytest.raises(TypeError, match="received NoneType"):
            external.finish(turn_id="saved", item_id="item", events=events)
        with external.reading() as original:
            assert not original.has_records(original.cut().offset)
        assert not root.exists()
        return
    external.finish(turn_id="saved", item_id="item", events=events)
    with CheckRoot(source, (owner.name,), root).open() as copied:
        session = Path(copied.registry.require(owner.name).session_file)
        rows = [NativeEntry.read(line) for line in session.read_bytes().splitlines()]
        assert [entry.message.text for entry in rows[1:]] == [event.text for event in events[:2]]
        assert [entry.timestamp.at.timestamp() for entry in rows[1:]] == [
            event.timestamp for event in events[:2]
        ]
    with external.reading() as original:
        assert tuple(original.reverse(original.cut().offset))[0].events == events
    assert not root.exists()


def test_wrapped_nonzero_exit_removes_root_and_is_the_cli_exit(source, tmp_path, capsys):
    root = tmp_path / "command"
    status = main(["--root", str(source.root), "check-root", "--threads", "B", "--into", str(root),
                   "--", sys.executable, "-c",
                   "import pathlib,sys; assert pathlib.Path(sys.argv[1]).is_dir(); sys.exit(7)", "{root}"])
    assert status == 7
    assert json.loads(capsys.readouterr().out)["return_code"] == 7
    assert not root.exists()


def test_python_failure_removes_root(source, tmp_path):
    root = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="failed check"):
        with CheckRoot(source, ("B",), root).open():
            raise RuntimeError("failed check")
    assert not root.exists()


@pytest.mark.parametrize("release", (False, True))
def test_initial_history_retains_display_not_source_claim_authority(source, tmp_path, release):
    with source.bus.log.opened_wire_snapshot() as history:
        message = next(history.newest_messages())
    owner = source.registry.require(message.sender).incarnation.require_recorded()
    resource = str(tmp_path / "retained.py")
    identity = dict(owner=message.sender, incarnation=owner.lookup,
                    seq=message.seq, message_id=message.message_id)
    if release:
        transition = ReleasedClaims(**identity, releases=(ClaimRelease(resource, "a" * 32),))
    else:
        transition = WakeClaims(
            **identity, claims=(resource,), generation="a" * 32,
            claim_scope=NativeClaim(owner_claimed_generation=1, operation_id="b" * 32,
                                    turn_id="history-copy", input_id="history-copy"),
        )
    retained = ClaimMessage.of(message, transition)

    class ClaimHistoryCheckRoot(CheckRoot):
        def _populate(self, copied):
            copied.messaging.initialize_private_initial_protocol(history=(retained,))

    root = tmp_path / "claim-history"
    with ClaimHistoryCheckRoot(source, ("A", "B"), root).open() as copied:
        with copied.bus.log.opened_wire_snapshot() as history:
            assert next(history.newest_messages()) == retained.history_snapshot
        assert retained.claim_transition is transition
    assert not root.exists()
    assert source.registry.require("A").name == "A"
    with source.bus.log.opened_wire_snapshot() as history:
        assert next(history.newest_messages()) == message


def test_cleanup_uses_the_registry_executor_selection_not_human_senders(source, tmp_path):
    root = tmp_path / "with-human"
    with CheckRoot(source, ("B",), root).open() as c:
        user = c.messaging.user_identity(c.registry.require("B").worktree)
        assert user.name in c.registry.snapshot().threads
    assert not root.exists()


def test_existing_destination_and_source_directory_are_never_removed(source, tmp_path):
    with pytest.raises(FileExistsError):
        with CheckRoot(source, ("B",), tmp_path).open():
            pytest.fail("existing directory was entered")
    assert source.root.is_dir()
    with pytest.raises(ValueError, match="inside its source"):
        with CheckRoot(source, ("B",), source.root / "nested").open():
            pytest.fail("source directory was entered")
    assert source.registry.require("B").name == "B"


def test_new_uncertain_input_is_kept_instead_of_erased(source, tmp_path):
    root = tmp_path / "uncertain"
    with pytest.raises(RecoveryBlocked, match="uncertain work kept"):
        with CheckRoot(source, ("B",), root).open() as c:
            InputDispositions(c.root).record("synthetic-unknown", seq=None, owner="B",
                claim_scope=1, target="B", text="construction-only uncertainty fixture")
    assert not root.exists()
    archive, = tmp_path.glob("uncertain.unknown-*")
    assert InputDispositions(archive).lookup("synthetic-unknown").unresolved
    assert list((archive / "native-sessions").rglob("*.jsonl"))
    # No provider/tool was run for this synthetic row.
    shutil.rmtree(archive)


def test_external_records_open_without_borrowing_a_native_entry(source, tmp_path):
    transcript = source.transcripts.external('B')
    transcript.finish(turn_id='external-turn', item_id='reply',
                      events=(AssistantTranscript('external reply', timestamp=1.0, position='reply'),))
    with transcript.reading() as reader:
        record, = reader.forward(0, reader.cut().offset)
        assert record.project(lambda native: pytest.fail('external record is not native')) == ()
    turns = TurnDigestRead(source.root, source.registry.require('B'), LastTurns()).native().turns
    assert [turn.turn_id for turn in turns] == ['external-turn']
    assert [event.text for event in turns[0].events] == ['external reply']
    root = tmp_path / 'external-window'
    with CheckRoot(source, ('B',), root).open() as copied:
        assert copied.registry.require('B').name == 'B'
        assert Path(copied.registry.require('B').session_file).is_file()
    assert not root.exists()
    assert transcript.turns(LastTurns()) == turns


def test_native_window_uses_existing_import_and_route_readers(source, tmp_path, monkeypatch):
    session = create_fresh_private_session(tmp_path / "original-session", worktree=tmp_path)
    stamp = "2026-10-19T14:00:00Z"
    with session.path.open("a") as output:
        for i in range(20):
            record = {"type": "message", "id": f"entry-{i}", "parentId": None if i == 0 else f"entry-{i-1}",
                      "timestamp": stamp, "message": {"role": "user", "content": f"history {i}", "timestamp": 1}}
            output.write(json.dumps(record) + "\n")
    source.registry.register(replace(source.registry.require("B"), session_file=session.session_file), StoppedThreadStatus())
    monkeypatch.setattr(CheckRoot, "max_records", 3)
    original = session.path.read_bytes()
    root = tmp_path / "native-window"
    with CheckRoot(source, ("B",), root).open() as c:
        copied = Path(c.registry.require("B").session_file)
        rows = [NativeEntry.read(line) for line in copied.read_bytes().splitlines()]
        assert len(rows) == 4
        assert rows[0].id != session.session_id
        assert [entry.message.text for entry in rows[1:]] == ["history 17", "history 18", "history 19"]
        assert rows[1].parent_id is None
        assert rows[2].parent_id == rows[1].id
        source_time = NativeEntry.read(original.splitlines()[1]).timestamp.at
        assert all(entry.timestamp.at == source_time for entry in rows[1:])
        assert session.path.read_bytes() == original
    assert not root.exists()
