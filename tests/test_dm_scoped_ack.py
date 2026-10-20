"""No-provider bounded human DM read evidence; never use global inbox ACK."""

from __future__ import annotations
from agent_comms.channel_targets import CommandTarget
from agent_comms.registration import Registration

import os
import shutil
from pathlib import Path

import pytest

from agent_comms.comms import wire
from agent_comms.read_basis import DMSelection, EmptyTail
from agent_comms.coordination_errors import DMPaintedBoundChanged, DMScopeChanged, TranscriptChanged
from agent_comms.child_process import ProcessIdentity
from agent_comms.threads import Thread
from agent_comms.message_page import PageBounds


def _thread(root: Path, name: str) -> Thread:
    thread_registration = Registration(Path(root) / "registry.json")
    return thread_registration.new_thread(name, frozenset({'talk'}), str(root), process_identity=ProcessIdentity.capture(os.getpid()))


def test_dm_page_marks_covered_range_preserving_omitted_older_unread(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_thread(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    for index in range(50):
        comms.messaging.send("peer", viewer, f"old {index}").message.message_id
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path), bounds=PageBounds(limit=5))
    assert len(page.messages) == 5 and page.has_older
    assert isinstance(page.selection, DMSelection)
    comms.views.mark_page_read(page, worktree=str(tmp_path))
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 45
    assert [message.body for message in comms.bus.inbox(viewer, CommandTarget.named("peer"))] == [
        f"old {index}" for index in range(45)
    ]


def test_peer_delete_same_name_rebind_rejects_stale_page_without_read_ack(tmp_path: Path):
    comms = wire(tmp_path)
    for name in ("peer", "other"):
        comms.registry.declare(_thread(tmp_path, name))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("other", viewer, "OTHER_UNPAINTED_1").message.message_id
    comms.messaging.send("peer", viewer, "PEER_PAINTED_2").message.message_id
    comms.messaging.send("other", viewer, "OTHER_UNPAINTED_3").message.message_id
    comms.messaging.send("other", "#all", "OTHER_CHANNEL_4").message.message_id
    comms.messaging.send("peer", viewer, "PEER_PAINTED_5").message.message_id
    comms.messaging.send("other", viewer, "OTHER_UNPAINTED_6").message.message_id
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert [message.body for message in page.messages] == ["PEER_PAINTED_2", "PEER_PAINTED_5"]
    assert page.newest_seq == 5 and isinstance(page.selection, DMSelection)
    comms.registry.unregister("peer")
    comms.registry.remove("peer")
    comms.threads.rename_managed_thread("other", "peer", owner_pid=os.getpid())
    before = comms.bus.pending_count(viewer, CommandTarget.named("peer"))
    marker_path = tmp_path / "read_ledger.json"
    markers_before = marker_path.read_bytes() if marker_path.exists() else None
    assert before == 3
    with pytest.raises(DMScopeChanged):
        comms.views.mark_page_read(page, worktree=str(tmp_path))
    markers_after = marker_path.read_bytes() if marker_path.exists() else None
    assert markers_after == markers_before
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == before


def test_foreign_root_wrong_peer_and_unbounded_or_bool_through_rejected(tmp_path: Path):
    first, second = wire(tmp_path / "first"), wire(tmp_path / "second")
    for comms in (first, second):
        for name in ("peer", "other"):
            comms.registry.declare(_thread(comms.root, name))
        viewer = comms.messaging.user_identity(str(comms.root)).name
        comms.messaging.send("peer", viewer, "painted").message.message_id
    page = first.views.dm_display_page("peer", worktree=str(first.root))
    assert isinstance(page.selection, DMSelection) and page.newest_seq is not None
    for comms, target, through, error in (
        (second, "peer", page.newest_seq, DMScopeChanged),
        (first, "other", page.newest_seq, DMScopeChanged),
        (first, "peer", page.newest_seq + 1, DMPaintedBoundChanged),
        (first, "peer", True, ValueError),
        (first, "peer", -1, ValueError),
    ):
        with pytest.raises(error):
            page.selection.basis.acknowledge(
                comms.bus, comms.registry, comms.root, target,
                worktree=str(comms.root), through=through,
            )
    assert first.bus.pending_count(first.messaging.user_identity(str(first.root)).name, CommandTarget.named("peer")) == 1


def test_painted_bound_uses_existing_transcript_refresh_error(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_thread(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "painted").message.message_id
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    with pytest.raises(TranscriptChanged):
        page.selection.basis.acknowledge(
            comms.bus, comms.registry, comms.root, "peer",
            worktree=str(tmp_path), through=page.newest_seq + 1,
        )
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 1


def test_root_replaced_after_fetch_rejects_mark_without_reading_new_root(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_thread(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "painted in original root").message.message_id
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    previous = tmp_path.with_name(tmp_path.name + "-previous")
    tmp_path.rename(previous)
    shutil.copytree(previous, tmp_path, symlinks=True)
    with pytest.raises(DMScopeChanged):
        comms.views.mark_page_read(page, worktree=str(tmp_path))
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 1


def test_independent_read_is_idempotent_and_never_retargets_page(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_thread(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "painted").message.message_id
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    comms.views.mark_user_view_read("peer", worktree=str(tmp_path))
    comms.messaging.send("peer", viewer, "unpainted after explicit mark").message.message_id
    comms.views.mark_page_read(page, worktree=str(tmp_path))
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 1


def test_empty_dm_basis_is_no_op_without_creating_read_ledger(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_thread(tmp_path, "peer"))
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert page.selection.basis.displayed.tail == EmptyTail()
    page.selection.basis.acknowledge(
        comms.bus, comms.registry, comms.root, "peer", worktree=str(tmp_path), through=0,
    )
    assert not (tmp_path / "read_ledger.json").exists()


    # A failure at the *final* post-replace sync remains UNKNOWN: absent a
    # separate durable marker commit witness, the row may already be visible.
