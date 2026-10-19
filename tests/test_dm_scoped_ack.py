"""No-provider bounded human DM read evidence; never use global inbox ACK."""

from __future__ import annotations
from agent_comms.channel_targets import CommandTarget
from agent_comms.registration import Registration

import os
import stat
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_comms.locked_store as locked_store
from agent_comms.comms import wire
from agent_comms.read_basis import DMDisplayBasis, EmptyTail
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
        comms.messaging.send("peer", viewer, f"old {index}")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path), bounds=PageBounds(limit=5))
    assert len(page.messages) == 5 and page.has_older
    assert page.display_basis is not None
    comms.views.mark_dm_view_read(
        "peer", worktree=str(tmp_path), through=page.newest_seq,
        expected_display_basis=page.display_basis,
    )
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 45
    assert [message.body for message in comms.bus.inbox(viewer, CommandTarget.named("peer"))] == [
        f"old {index}" for index in range(45)
    ]


def test_deliberate_baseline_and_painted_page_ack_only_peer_through_bound(tmp_path: Path):
    comms = wire(tmp_path)
    for name in ("peer", "other", "executor"):
        comms.registry.declare(_thread(tmp_path, name))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    for index in range(50):
        comms.messaging.send("peer", viewer, f"old {index}")
    comms.views.mark_user_view_read("peer", worktree=str(tmp_path))
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 0
    comms.messaging.send("peer", viewer, "painted")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path), bounds=PageBounds(limit=5))
    proof = page.display_basis
    assert isinstance(proof, DMDisplayBasis)
    painted = page.newest_seq
    assert painted is not None
    comms.messaging.send("other", viewer, "other unread")
    comms.messaging.send("peer", "executor", "executor unread")
    comms.messaging.send("peer", "#all", "channel unread")
    comms.messaging.send("peer", viewer, "after paint")
    comms.views.mark_dm_view_read(
        "peer",
        worktree=str(tmp_path),
        through=painted,
        expected_display_basis=proof,
    )
    reopened = wire(tmp_path)
    assert [message.body for message in reopened.bus.inbox(viewer, CommandTarget.named("peer"))] == ["after paint"]
    assert reopened.bus.pending_count(viewer, CommandTarget.named("peer")) == 1
    assert reopened.bus.pending_count(viewer, CommandTarget.named("other")) == 1
    assert reopened.bus.pending_count("executor", CommandTarget.named("peer")) == 1
    assert reopened.bus.pending_count("executor", CommandTarget.named("#all")) == 1
    # No global marker is created by this human DM read transition.
    seen = reopened.bus.reads.cursor(viewer, reopened.registry.snapshot())
    assert painted in seen and painted + 1 not in seen
    assert not (tmp_path / "read_markers.json").exists()


def test_peer_delete_same_name_rebind_rejects_stale_page_without_read_ack(tmp_path: Path):
    comms = wire(tmp_path)
    for name in ("peer", "other"):
        comms.registry.declare(_thread(tmp_path, name))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("other", viewer, "OTHER_UNPAINTED_1")
    comms.messaging.send("peer", viewer, "PEER_PAINTED_2")
    comms.messaging.send("other", viewer, "OTHER_UNPAINTED_3")
    comms.messaging.send("other", "#all", "OTHER_CHANNEL_4")
    comms.messaging.send("peer", viewer, "PEER_PAINTED_5")
    comms.messaging.send("other", viewer, "OTHER_UNPAINTED_6")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert [message.body for message in page.messages] == ["PEER_PAINTED_2", "PEER_PAINTED_5"]
    assert page.newest_seq == 5 and page.display_basis is not None
    comms.registry.unregister("peer")
    comms.registry.remove("peer")
    comms.threads.rename_managed_thread("other", "peer", owner_pid=os.getpid())
    before = comms.bus.pending_count(viewer, CommandTarget.named("peer"))
    marker_path = tmp_path / "read_ledger.json"
    markers_before = marker_path.read_bytes() if marker_path.exists() else None
    assert before == 3
    with pytest.raises(DMScopeChanged, match="registry changed|incarnation changed"):
        comms.views.mark_dm_view_read(
            "peer",
            worktree=str(tmp_path),
            through=5,
            expected_display_basis=page.display_basis,
        )
    markers_after = marker_path.read_bytes() if marker_path.exists() else None
    assert markers_after == markers_before
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == before


def test_foreign_root_wrong_peer_and_unbounded_or_bool_through_rejected(tmp_path: Path):
    first, second = wire(tmp_path / "first"), wire(tmp_path / "second")
    for comms in (first, second):
        for name in ("peer", "other"):
            comms.registry.declare(_thread(comms.root, name))
        viewer = comms.messaging.user_identity(str(comms.root)).name
        comms.messaging.send("peer", viewer, "painted")
    page = first.views.dm_display_page("peer", worktree=str(first.root))
    assert page.display_basis is not None and page.newest_seq is not None
    for comms, target, through, error in (
        (second, "peer", page.newest_seq, DMScopeChanged),
        (first, "other", page.newest_seq, DMScopeChanged),
        (first, "peer", page.newest_seq + 1, DMPaintedBoundChanged),
        (first, "peer", True, ValueError),
        (first, "peer", -1, ValueError),
    ):
        with pytest.raises(error):
            comms.views.mark_dm_view_read(
                target,
                worktree=str(comms.root),
                through=through,
                expected_display_basis=page.display_basis,
            )
    assert first.bus.pending_count(first.messaging.user_identity(str(first.root)).name, CommandTarget.named("peer")) == 1


def test_painted_bound_uses_existing_transcript_refresh_error(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_thread(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "painted")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    with pytest.raises(TranscriptChanged):
        comms.views.mark_dm_view_read(
            "peer", worktree=str(tmp_path), through=page.newest_seq + 1,
            expected_display_basis=page.display_basis,
        )
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 1


def test_root_replaced_after_fetch_rejects_mark_without_reading_new_root(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_thread(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "painted in original root")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    previous = tmp_path.with_name(tmp_path.name + "-previous")
    tmp_path.rename(previous)
    shutil.copytree(previous, tmp_path, symlinks=True)
    with pytest.raises(DMScopeChanged, match="root was replaced"):
        comms.views.mark_dm_view_read(
            "peer", worktree=str(tmp_path), through=page.newest_seq,
            expected_display_basis=page.display_basis,
        )
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 1


def test_independent_read_is_idempotent_and_never_retargets_page(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_thread(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "painted")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    comms.views.mark_user_view_read("peer", worktree=str(tmp_path))
    comms.messaging.send("peer", viewer, "unpainted after explicit mark")
    comms.views.mark_dm_view_read(
        "peer",
        worktree=str(tmp_path),
        through=page.newest_seq,
        expected_display_basis=page.display_basis,
    )
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 1


def test_empty_dm_basis_is_no_op_without_creating_read_ledger(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_thread(tmp_path, "peer"))
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert page.display_basis.displayed.tail == EmptyTail()
    comms.views.mark_dm_view_read(
        "peer", worktree=str(tmp_path), through=0, expected_display_basis=page.display_basis,
    )
    assert not (tmp_path / "read_ledger.json").exists()


@pytest.fixture
def durable_root(tmp_path):
    yield tmp_path


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync")
def test_scoped_marker_fsyncs_parent_and_sync_denial_is_not_success(durable_root, monkeypatch):
    tmp_path = durable_root
    comms = wire(tmp_path)
    comms.registry.declare(_thread(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "painted")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert page.display_basis is not None
    original = os.fsync
    parent_fd_seen: list[int] = []

    def observe(fd: int) -> None:
        metadata = os.fstat(fd)
        if stat.S_ISDIR(metadata.st_mode) and metadata.st_ino == tmp_path.stat().st_ino:
            parent_fd_seen.append(fd)
        original(fd)

    monkeypatch.setattr(locked_store, "os", SimpleNamespace(**(vars(os) | {"fsync": observe})))
    comms.views.mark_dm_view_read(
        "peer",
        worktree=str(tmp_path),
        through=page.newest_seq,
        expected_display_basis=page.display_basis,
    )
    assert parent_fd_seen and comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 0

    comms.messaging.send("peer", viewer, "next painted")
    next_page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert next_page.display_basis is not None

    def deny_parent(fd: int) -> None:
        metadata = os.fstat(fd)
        if stat.S_ISDIR(metadata.st_mode) and metadata.st_ino == tmp_path.stat().st_ino:
            raise OSError("injected parent fsync denial")
        original(fd)

    marker_before = (tmp_path / "read_ledger.json").read_bytes()
    monkeypatch.setattr(
        locked_store, "os", SimpleNamespace(**(vars(os) | {"fsync": deny_parent}))
    )
    with pytest.raises(OSError, match="injected parent fsync denial"):
        comms.views.mark_dm_view_read(
            "peer",
            worktree=str(tmp_path),
            through=next_page.newest_seq,
            expected_display_basis=next_page.display_basis,
        )
    assert (tmp_path / "read_ledger.json").read_bytes() == marker_before
    monkeypatch.setattr(locked_store, "os", os)
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 1
    # A failure at the *final* post-replace sync remains UNKNOWN: absent a
    # separate durable marker commit witness, the row may already be visible.
