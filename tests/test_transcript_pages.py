import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from agent_comms.acp_extension import TranscriptSnapshotUpdate
from agent_comms.coordination.errors import TranscriptChanged
from agent_comms.execution.session_file import RegistrySessionLocation
from agent_comms.execution.transcript_sources import (
    EarlierThan,
    LaterThan,
    NativeTranscriptCursor,
    Through,
    WireTranscriptCursor,
)
from agent_comms.execution.turn_item import (
    NativeItemId,
    NativeTurnId,
)
from agent_comms.messaging.bus_read import UiRead
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.registry.thread_text import (
    ForkedParent,
    GivenThreadText,
)
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.storage.private_path import FileIdentity, MissingFile
from agent_comms.transcript.transcript_events import AssistantTranscript
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.reads import TranscriptPageRead
from agent_comms.transport.tools.served import invoke_tool
from agent_comms.ui_model.observation import ReadTranscript
from conftest import installed_comms, transcript_records


def transcript(path, count):
    path.write_text(
        json.dumps(transcript_records.session(id="session").to_wire())
        + "\n"
        + "".join(
            json.dumps(
                transcript_records.message({"role": "assistant", "content": [{"type": "text", "text": f"Record {i}"}]})
            )
            + "\n"
            for i in range(count)
        )
    )


def test_pages_reach_beginning_and_return_to_tail_without_duplicates(tmp_path):
    path = tmp_path / "session.jsonl"
    transcript(path, 105)
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(NativeThread('worker', frozenset(), Checkout(tmp_path), session_file=RegistrySessionLocation.from_path(str(path), comms.root)))
    page = comms.transcripts.capture_page_read("worker").read()
    through = page.after
    seen = [event.text for event in page.events]
    assert seen == [f"Record {i}" for i in range(85, 105)]
    while page.has_older:
        page = comms.transcripts.capture_page_read(
            "worker", position=EarlierThan(page.before)).read()
        seen[:0] = [event.text for event in page.events]
    assert seen == [f"Record {i}" for i in range(105)]
    assert page.before.offset == 0
    # New live output must not leak into the frozen replay window and duplicate ACP input.
    with path.open("a") as output:
        output.write(
            json.dumps(transcript_records.message({"role": "assistant", "content": [{"type": "text", "text": "LIVE"}]}))
            + "\n"
        )
    seen = [event.text for event in page.events]
    while page.has_newer:
        page = comms.transcripts.capture_page_read(
            "worker", position=LaterThan(page.after), limit=Through(through)).read()
        seen.extend(event.text for event in page.events)
    assert seen == [f"Record {i}" for i in range(105)]


def shared_page(comms, operation: TranscriptPageRead) -> TranscriptSnapshotUpdate:
    """One page operation served directly, inside the UI's root-bound envelope and through MCP:
    all three carry the same page, draft and witness as the domain capture."""
    expected = TranscriptSnapshotUpdate.from_read(comms.transcripts.capture_page_read(
        operation.thread, position=operation.position, limit=operation.limit,
        page_source=operation.page_source))
    direct = UiRead.serving(operation).read(comms)
    assert ReadTranscript(str(comms.root), operation).read(comms) == direct == expected
    assert invoke_tool(comms, operation.declared_name, FieldCodec.encode_fields(operation),
                       caller=ThreadCaller(operation.thread)) == FieldCodec.encode(direct)
    return direct


def test_one_page_operation_pages_alike_for_the_ui_and_mcp(tmp_path):
    path = tmp_path / "session.jsonl"
    transcript(path, 45)
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(NativeThread('worker', frozenset(), Checkout(tmp_path), session_file=RegistrySessionLocation.from_path(str(path), comms.root)))
    snapshot = shared_page(comms, TranscriptPageRead(thread="worker"))
    through = snapshot.page.after
    seen = [event.text for event in snapshot.page.events]
    pages = 1
    while snapshot.page.has_older:
        snapshot = shared_page(comms, TranscriptPageRead(thread="worker", position=EarlierThan(snapshot.page.before)))
        seen[:0] = [event.text for event in snapshot.page.events]
        pages += 1
    assert pages > 1 and seen == [f"Record {i}" for i in range(45)]
    seen = [event.text for event in snapshot.page.events]
    while snapshot.page.has_newer:
        snapshot = shared_page(comms, TranscriptPageRead(
            thread="worker", position=LaterThan(snapshot.page.after), limit=Through(through)))
        seen.extend(event.text for event in snapshot.page.events)
    assert seen == [f"Record {i}" for i in range(45)]
    comms.registry.declare(NativeThread('quiet', frozenset(), Checkout(tmp_path)))
    empty = shared_page(comms, TranscriptPageRead(thread="quiet"))
    assert not empty.page.events and not empty.page.has_older and not empty.page.has_newer


def test_oversized_message_and_file_identity(tmp_path):
    path = tmp_path / "session.jsonl"
    text = "x" * 200000
    path.write_text(
        json.dumps(transcript_records.message({"role": "assistant", "content": [{"type": "text", "text": text}]})) + "\n"
    )
    comms = installed_comms(tmp_path / "wire")
    thread = NativeThread('worker', frozenset(), Checkout(tmp_path), session_file=RegistrySessionLocation.from_path(str(path), comms.root))
    comms.registry.declare(thread)
    page = comms.transcripts.capture_page_read("worker").read()
    assert page.events[0].text == text
    assert not page.has_older
    comms.registry.declare(replace(thread, session_file=RegistrySessionLocation.from_path(str(tmp_path / "other.jsonl"), comms.root)))
    with pytest.raises(TranscriptChanged):
        comms.transcripts.capture_page_read("worker", position=EarlierThan(page.before)).read()
    with pytest.raises(TranscriptChanged):
        comms.transcripts.capture_page_read("worker", position=EarlierThan(NativeTranscriptCursor(str(path), -1, file=MissingFile()))).read()


def test_a_cursor_into_a_replaced_session_is_refused_even_where_its_offset_still_fits(tmp_path):
    path = tmp_path / "session.jsonl"
    transcript(path, 30)
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(NativeThread('worker', frozenset(), Checkout(tmp_path),
                                        session_file=RegistrySessionLocation.from_path(str(path), comms.root)))
    page = comms.transcripts.capture_page_read("worker").read()
    # A producer that rewrites rather than appends: a longer file under a new inode, so the
    # old offsets still fit inside it but no longer land on its record boundaries.
    rewritten = tmp_path / "session.jsonl.tmp"
    transcript(rewritten, 45)
    os.replace(rewritten, path)
    assert path.stat().st_size > page.after.offset
    with pytest.raises(TranscriptChanged):
        comms.transcripts.capture_page_read("worker", position=LaterThan(page.after)).read()
    with pytest.raises(TranscriptChanged):
        comms.transcripts.capture_page_read("worker", position=EarlierThan(page.before)).read()
    with pytest.raises(TranscriptChanged):
        comms.views.mark_thread_view_read("worker", worktree=Checkout(tmp_path), through=page.after)
    latest = comms.transcripts.capture_page_read("worker").read()
    assert [event.text for event in latest.events][-1] == "Record 44"
    comms.views.mark_thread_view_read("worker", worktree=Checkout(tmp_path), through=latest.after)


def test_empty_transcript_and_non_message_records(tmp_path):
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(NativeThread('worker', frozenset(), Checkout(tmp_path)))
    page = comms.transcripts.capture_page_read("worker").read()
    assert not page.events and not page.has_older and not page.has_newer


def test_new_fork_projects_parent_history_and_instruction_until_own_session_exists(tmp_path):
    parent_path = tmp_path / "parent.jsonl"
    transcript(parent_path, 3)
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(
        NativeThread('parent', frozenset(), Checkout(tmp_path), session_file=RegistrySessionLocation.from_path(str(parent_path), comms.root))
    )
    comms.registry.declare(
        NativeThread('child', frozenset(), Checkout(tmp_path), parent=ForkedParent('parent'), task=GivenThreadText('Inspect the renderer'))
    )

    page = comms.transcripts.capture_page_read("child").read()
    assert page.after.session_file == str(parent_path)
    assert [event.text for event in page.events] == [
        "Record 0",
        "Record 1",
        "Record 2",
        "Forked from @parent. This thread started with:",
        "Inspect the renderer",
    ]
    assert [event.declared_name for event in page.events[-2:]] == ["fork_origin", "fork_task"]
    assert [event.text for event in comms.transcripts.capture_page_read("child").read().events][-2:] == [
        "Forked from @parent. This thread started with:",
        "Inspect the renderer",
    ]

    child_path = tmp_path / "child.jsonl"
    transcript(child_path, 1)
    comms.threads.attach_session((attachment_owner := comms.registry.require("child")), str(child_path), process=attachment_owner.process_identity)
    assert [event.text for event in comms.transcripts.capture_page_read("child").read().events] == [
        "Record 0"
    ]


def test_inherited_scroll_window_survives_child_session_persistence(tmp_path):
    parent_path = tmp_path / "parent.jsonl"
    transcript(parent_path, 35)
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(
        NativeThread('parent', frozenset(), Checkout(tmp_path), session_file=RegistrySessionLocation.from_path(str(parent_path), comms.root))
    )
    comms.registry.declare(
        NativeThread('child', frozenset(), Checkout(tmp_path), parent=ForkedParent('parent'), task=GivenThreadText('Child task'))
    )
    page = comms.transcripts.capture_page_read("child").read()
    through = page.after
    own_path = tmp_path / "child.jsonl"
    transcript(own_path, 1)
    comms.threads.attach_session((attachment_owner := comms.registry.require("child")), str(own_path), process=attachment_owner.process_identity)
    seen = [event.text for event in page.events if event.declared_name == "assistant"]
    while page.has_older:
        page = comms.transcripts.capture_page_read(
            "child", position=EarlierThan(page.before), limit=Through(through)).read()
        seen[:0] = [event.text for event in page.events if event.declared_name == "assistant"]
    assert seen == [f"Record {i}" for i in range(35)]
    assert comms.transcripts.capture_page_read("child").read().after.session_file == str(own_path)
    unrelated = tmp_path / "unrelated.jsonl"
    transcript(unrelated, 1)
    with pytest.raises(TranscriptChanged):
        comms.transcripts.capture_page_read(
            "child", limit=Through(NativeTranscriptCursor(str(unrelated), unrelated.stat().st_size,
                                                file=FileIdentity.from_stat(unrelated.stat())))).read()


def test_wire_cursor_keeps_the_furthest_committed_native_prefix_within_it():
    from agent_comms.execution.transcript_sources import AssignedSourceCursor

    wire_at = AssignedSourceCursor("root", RecordedThreadIncarnation("recipient", 1.0), 5).at
    cursor = WireTranscriptCursor("a.jsonl", 900, wire_at(5), file=MissingFile())
    committed = (WireTranscriptCursor("a.jsonl", 40, wire_at(2), file=MissingFile()),
                 WireTranscriptCursor("a.jsonl", 70, wire_at(5), file=MissingFile()),
                 WireTranscriptCursor("a.jsonl", 99, wire_at(6), file=MissingFile()),
                 WireTranscriptCursor("b.jsonl", 500, wire_at(1), file=MissingFile()))
    assert cursor.over_native(committed) == WireTranscriptCursor("a.jsonl", 70, wire_at(5), file=MissingFile())
    assert cursor.over_native(committed[3:]) == WireTranscriptCursor("b.jsonl", 500, wire_at(5), file=MissingFile())
    assert cursor.over_native(()) == WireTranscriptCursor("a.jsonl", 0, wire_at(5), file=MissingFile())


def test_never_started_native_child_keeps_no_native_cursor_and_no_read_mark(tmp_path):
    from agent_comms.cli_verbs.register import RegisterCliCommand
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.execution.transcript_sources import TranscriptCursor
    from agent_comms.transcript.transcripts import NoNativeTranscriptCursor

    path = tmp_path / "parent.jsonl"
    transcript(path, 3)
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(NativeThread(
        "parent", frozenset(), Checkout(tmp_path), session_file=RegistrySessionLocation.from_path(str(path), comms.root)))
    RegisterCliCommand(name="child", worktree=Checkout(tmp_path), parent=ForkedParent("parent"),
                       execution=NativeThread).apply(comms)
    checkpoint = comms.transcripts.transcript_checkpoint("child")
    inherited = comms.transcripts.capture_page_read("child").read().after
    painted = checkpoint.over_native((inherited,))
    assert painted == NoNativeTranscriptCursor(source=comms.registry.require("child").runtime,
                                               wire=checkpoint.wire, file=MissingFile())
    assert inherited == NoNativeTranscriptCursor(source=comms.registry.require("parent").runtime,
                                                 offset=path.stat().st_size, wire=checkpoint.wire,
                                                 file=FileIdentity.from_stat(path.stat()))
    assert FieldCodec.decode(TranscriptCursor, FieldCodec.encode(painted)) == painted
    ledger = comms.bus.reads.path
    before = ledger.read_bytes() if ledger.exists() else b""
    comms.views.mark_thread_view_read("child", worktree=Checkout(tmp_path), through=painted)
    comms.views.mark_thread_view_read("child", worktree=Checkout(tmp_path), through=inherited)
    assert (ledger.read_bytes() if ledger.exists() else b"") == before

    parent = comms.transcripts.transcript_checkpoint("parent")
    unpainted = replace(checkpoint, wire=parent.wire)
    assert parent.over_native((unpainted,)) == unpainted
    comms.views.mark_thread_view_read("parent", worktree=Checkout(tmp_path), through=unpainted)
    assert (ledger.read_bytes() if ledger.exists() else b"") == before
    comms.views.mark_thread_view_read("parent", worktree=Checkout(tmp_path), through=parent)
    viewer = comms.messaging.user_identity(Checkout(tmp_path)).name
    assert comms.bus.reads.transcript_seen(viewer, str(path), path.stat().st_ino) == path.stat().st_size

    # The same cursor state retains its own external record coordinate, not a parent's.
    own = replace(checkpoint, offset=30)
    assert own.over_native((replace(own, offset=12), inherited)) == replace(own, offset=12)


def test_fork_header_stays_at_the_fork_point_while_the_parent_grows(tmp_path):
    """A fork shows its parent's history up to the fork, then its header, once: the parent's
    later work is not the fork's history, and a later read must not move the header after it."""
    parent_path = tmp_path / "parent.jsonl"
    transcript(parent_path, 3)
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(
        NativeThread('parent', frozenset(), Checkout(tmp_path), session_file=RegistrySessionLocation.from_path(str(parent_path), comms.root))
    )
    comms.registry.declare(
        NativeThread('child', frozenset(), Checkout(tmp_path), parent=ForkedParent('parent'), task=GivenThreadText('Inspect the renderer'))
    )
    forked_view = [
        "Record 0",
        "Record 1",
        "Record 2",
        "Forked from @parent. This thread started with:",
        "Inspect the renderer",
    ]
    assert [event.text for event in comms.transcripts.capture_page_read("child").read().events] == forked_view

    after_fork = datetime.fromtimestamp(comms.registry.require("child").incarnation.wire_birth + 60, UTC)
    with parent_path.open("a") as parent_file:
        for i in (3, 4):
            record = transcript_records.message(
                {"role": "assistant", "content": [{"type": "text", "text": f"Record {i}"}],
                 "timestamp": int(after_fork.timestamp() * 1000)})
            record["timestamp"] = after_fork.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            parent_file.write(json.dumps(record) + "\n")

    assert [event.text for event in comms.transcripts.capture_page_read("child").read().events] == forked_view


def test_a_fork_of_a_fork_stops_at_the_earlier_fork(tmp_path):
    """A grandchild sees its grandparent's history only up to the parent's fork, not its own."""
    root_path = tmp_path / "root.jsonl"
    transcript(root_path, 2)
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(
        NativeThread('root', frozenset(), Checkout(tmp_path), session_file=RegistrySessionLocation.from_path(str(root_path), comms.root))
    )
    comms.registry.declare(
        NativeThread('middle', frozenset(), Checkout(tmp_path), parent=ForkedParent('root'), task=GivenThreadText('Middle task'))
    )
    time.sleep(0.05)
    comms.registry.declare(
        NativeThread('leaf', frozenset(), Checkout(tmp_path), parent=ForkedParent('middle'), task=GivenThreadText('Leaf task'))
    )
    between_forks = datetime.fromtimestamp(comms.registry.require("middle").incarnation.wire_birth + 0.02, UTC)
    assert between_forks.timestamp() < comms.registry.require("leaf").incarnation.wire_birth
    with root_path.open("a") as root_file:
        record = transcript_records.message(
            {"role": "assistant", "content": [{"type": "text", "text": "After the middle fork"}],
             "timestamp": int(between_forks.timestamp() * 1000)})
        record["timestamp"] = between_forks.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        root_file.write(json.dumps(record) + "\n")

    assert [event.text for event in comms.transcripts.capture_page_read("leaf").read().events] == [
        "Record 0",
        "Record 1",
        "Forked from @middle. This thread started with:",
        "Leaf task",
    ]


def test_a_fork_of_an_external_thread_shows_its_own_work_after_the_fork_header(tmp_path):
    """An external fork's records are its own, and all of them come after its birth: every Claude
    session on a shared grant is one, and a date filter here empties its whole transcript."""
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(ExternalThread('grant', frozenset(), Checkout(tmp_path)))
    comms.registry.declare(
        ExternalThread('session', frozenset(), Checkout(tmp_path), parent=ForkedParent('grant'),
                       task=GivenThreadText('Join the bus'))
    )
    born = comms.registry.require("session").incarnation.wire_birth
    comms.transcripts.external('session').finish(
        turn_id=NativeTurnId('t1'), item_id=NativeItemId('i1'),
        events=(AssistantTranscript('My own work', position=NativeItemId('i1'), timestamp=born + 60),))

    assert [event.text for event in comms.transcripts.capture_page_read("session").read().events] == [
        "Forked from @grant. This thread started with:",
        "Join the bus",
        "My own work",
    ]


def test_a_fork_of_an_external_fork_also_shows_its_own_work(tmp_path):
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(ExternalThread('grant', frozenset(), Checkout(tmp_path)))
    comms.registry.declare(ExternalThread('middle', frozenset(), Checkout(tmp_path), parent=ForkedParent('grant')))
    comms.registry.declare(
        ExternalThread('leaf', frozenset(), Checkout(tmp_path), parent=ForkedParent('middle'),
                       task=GivenThreadText('Leaf task'))
    )
    born = comms.registry.require("leaf").incarnation.wire_birth
    comms.transcripts.external('leaf').finish(
        turn_id=NativeTurnId('t1'), item_id=NativeItemId('i1'),
        events=(AssistantTranscript('Leaf work', position=NativeItemId('i1'), timestamp=born + 60),))

    assert [event.text for event in comms.transcripts.capture_page_read("leaf").read().events] == [
        "Forked from @middle. This thread started with:",
        "Leaf task",
        "Leaf work",
    ]
