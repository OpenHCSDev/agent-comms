"""No-provider bounded human DM read evidence; never use global inbox ACK."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from agent_comms import Thread, wire
from agent_comms import declarations as declarations_module
from agent_comms.declarations import DMDisplayBasis


def _thread(root: Path, name: str) -> Thread:
    return Thread(name, frozenset({"talk"}), str(root), pid=os.getpid())


def test_dm_page_requires_deliberate_baseline_before_omitted_older_unread(tmp_path: Path):
    comms = wire(tmp_path)
    comms.register(_thread(tmp_path, "peer"))
    viewer = comms.user_identity(str(tmp_path)).name
    for index in range(50):
        comms.send("peer", viewer, f"old {index}")
    page = comms.dm_display_page("peer", worktree=str(tmp_path), limit=5)
    assert len(page.messages) == 5 and page.has_older
    assert page.display_basis is not None and page.display_basis.older_unread
    before = (
        (tmp_path / "read_markers.json").read_bytes()
        if (tmp_path / "read_markers.json").exists()
        else None
    )
    with pytest.raises(ValueError, match="contiguous"):
        comms.mark_dm_view_read(
            "peer",
            worktree=str(tmp_path),
            through=page.newest_seq,
            expected_display_basis=page.display_basis,
        )
    after = (
        (tmp_path / "read_markers.json").read_bytes()
        if (tmp_path / "read_markers.json").exists()
        else None
    )
    assert before == after
    assert comms.pending_count(viewer, "peer") == 50


def test_deliberate_baseline_and_painted_page_ack_only_peer_through_bound(tmp_path: Path):
    comms = wire(tmp_path)
    for name in ("peer", "other", "executor"):
        comms.register(_thread(tmp_path, name))
    viewer = comms.user_identity(str(tmp_path)).name
    for index in range(50):
        comms.send("peer", viewer, f"old {index}")
    comms.mark_user_view_read("peer", worktree=str(tmp_path))
    assert comms.pending_count(viewer, "peer") == 0
    comms.send("peer", viewer, "painted")
    page = comms.dm_display_page("peer", worktree=str(tmp_path), limit=5)
    proof = page.display_basis
    assert isinstance(proof, DMDisplayBasis) and not proof.older_unread
    painted = page.newest_seq
    assert painted is not None
    comms.send("other", viewer, "other unread")
    comms.send("peer", "executor", "executor unread")
    comms.send("peer", "#all", "channel unread")
    comms.send("peer", viewer, "after paint")
    comms.mark_dm_view_read(
        "peer",
        worktree=str(tmp_path),
        through=painted,
        expected_display_basis=proof,
    )
    reopened = wire(tmp_path)
    assert [message.body for message in reopened.inbox(viewer, "peer")] == ["after paint"]
    assert reopened.pending_count(viewer, "peer") == 1
    assert reopened.pending_count(viewer, "other") == 1
    assert reopened.pending_count("executor", "peer") == 1
    assert reopened.pending_count("executor", "#all") == 1
    # No global marker is created by this human DM read transition.
    markers = json.loads((tmp_path / "read_markers.json").read_text())
    assert markers.get(viewer, 0) == 0
    assert markers[comms.bus._marker_key(viewer, "peer")] == painted


def test_peer_delete_same_name_rebind_rejects_stale_page_without_read_ack(tmp_path: Path):
    comms = wire(tmp_path)
    for name in ("peer", "other"):
        comms.register(_thread(tmp_path, name))
    viewer = comms.user_identity(str(tmp_path)).name
    comms.send("other", viewer, "OTHER_UNPAINTED_1")
    comms.send("peer", viewer, "PEER_PAINTED_2")
    comms.send("other", viewer, "OTHER_UNPAINTED_3")
    comms.send("other", "#all", "OTHER_CHANNEL_4")
    comms.send("peer", viewer, "PEER_PAINTED_5")
    comms.send("other", viewer, "OTHER_UNPAINTED_6")
    page = comms.dm_display_page("peer", worktree=str(tmp_path))
    assert [message.body for message in page.messages] == ["PEER_PAINTED_2", "PEER_PAINTED_5"]
    assert page.newest_seq == 5 and page.display_basis is not None
    comms.registry.unregister("peer")
    comms.delete("peer")
    comms.rename_managed_thread("other", "peer", owner_pid=os.getpid())
    before = comms.pending_count(viewer, "peer")
    marker_path = tmp_path / "read_markers.json"
    markers_before = marker_path.read_bytes() if marker_path.exists() else None
    assert before == 3
    with pytest.raises(ValueError, match="registry changed|incarnation changed"):
        comms.mark_dm_view_read(
            "peer",
            worktree=str(tmp_path),
            through=5,
            expected_display_basis=page.display_basis,
        )
    markers_after = marker_path.read_bytes() if marker_path.exists() else None
    assert markers_after == markers_before
    assert comms.pending_count(viewer, "peer") == before


def test_foreign_root_wrong_peer_and_unbounded_or_bool_through_rejected(tmp_path: Path):
    first, second = wire(tmp_path / "first"), wire(tmp_path / "second")
    for comms in (first, second):
        for name in ("peer", "other"):
            comms.register(_thread(comms.root, name))
        viewer = comms.user_identity(str(comms.root)).name
        comms.send("peer", viewer, "painted")
    page = first.dm_display_page("peer", worktree=str(first.root))
    assert page.display_basis is not None and page.newest_seq is not None
    for comms, target, through in (
        (second, "peer", page.newest_seq),
        (first, "other", page.newest_seq),
        (first, "peer", page.newest_seq + 1),
        (first, "peer", True),
        (first, "peer", -1),
    ):
        with pytest.raises(ValueError):
            comms.mark_dm_view_read(
                target,
                worktree=str(comms.root),
                through=through,
                expected_display_basis=page.display_basis,
            )
    assert first.pending_count(first.user_identity(str(first.root)).name, "peer") == 1


def test_changed_marker_basis_rejects_implicit_page_ack(tmp_path: Path):
    comms = wire(tmp_path)
    comms.register(_thread(tmp_path, "peer"))
    viewer = comms.user_identity(str(tmp_path)).name
    comms.send("peer", viewer, "painted")
    page = comms.dm_display_page("peer", worktree=str(tmp_path))
    assert page.display_basis is not None
    comms.mark_user_view_read("peer", worktree=str(tmp_path))
    marker_before = (tmp_path / "read_markers.json").read_bytes()
    with pytest.raises(ValueError, match="marker changed"):
        comms.mark_dm_view_read(
            "peer",
            worktree=str(tmp_path),
            through=page.newest_seq,
            expected_display_basis=page.display_basis,
        )
    assert (tmp_path / "read_markers.json").read_bytes() == marker_before


@pytest.fixture
def durable_root():
    with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
        yield Path(directory)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync")
def test_scoped_marker_fsyncs_parent_and_sync_denial_is_not_success(durable_root, monkeypatch):
    tmp_path = durable_root
    comms = wire(tmp_path)
    comms.register(_thread(tmp_path, "peer"))
    viewer = comms.user_identity(str(tmp_path)).name
    comms.send("peer", viewer, "painted")
    page = comms.dm_display_page("peer", worktree=str(tmp_path))
    assert page.display_basis is not None
    original = declarations_module.os.fsync
    parent_fd_seen: list[int] = []

    def observe(fd: int) -> None:
        metadata = os.fstat(fd)
        if stat.S_ISDIR(metadata.st_mode) and metadata.st_ino == tmp_path.stat().st_ino:
            parent_fd_seen.append(fd)
        original(fd)

    monkeypatch.setattr(declarations_module.os, "fsync", observe)
    comms.mark_dm_view_read(
        "peer",
        worktree=str(tmp_path),
        through=page.newest_seq,
        expected_display_basis=page.display_basis,
    )
    assert parent_fd_seen and comms.pending_count(viewer, "peer") == 0

    comms.send("peer", viewer, "next painted")
    next_page = comms.dm_display_page("peer", worktree=str(tmp_path))
    assert next_page.display_basis is not None

    def deny_parent(fd: int) -> None:
        metadata = os.fstat(fd)
        if stat.S_ISDIR(metadata.st_mode) and metadata.st_ino == tmp_path.stat().st_ino:
            raise OSError("injected parent fsync denial")
        original(fd)

    marker_before = (tmp_path / "read_markers.json").read_bytes()
    monkeypatch.setattr(declarations_module.os, "fsync", deny_parent)
    with pytest.raises(OSError, match="injected parent fsync denial"):
        comms.mark_dm_view_read(
            "peer",
            worktree=str(tmp_path),
            through=next_page.newest_seq,
            expected_display_basis=next_page.display_basis,
        )
    assert (tmp_path / "read_markers.json").read_bytes() == marker_before
    assert comms.pending_count(viewer, "peer") == 1
    # A failure at the *final* post-replace sync remains UNKNOWN: absent a
    # separate durable marker commit witness, the row may already be visible.


def test_ordinary_metadata_rollback_must_not_hide_new_dm(tmp_path: Path):
    """A stale metadata sequence cannot hide a later DM behind a painted marker."""
    comms = wire(tmp_path)
    comms.register(_thread(tmp_path, "peer"))
    viewer = comms.user_identity(str(tmp_path)).name
    comms.send("peer", viewer, "first")
    comms.send("peer", viewer, "second")
    page = comms.dm_display_page("peer", worktree=str(tmp_path))
    assert page.display_basis is not None and page.newest_seq == 2
    comms.mark_dm_view_read(
        "peer",
        worktree=str(tmp_path),
        through=page.newest_seq,
        expected_display_basis=page.display_basis,
    )
    # Deterministically model a committed marker plus nonzero bus_meta
    # rollback after crash. The ordinary writer currently trusts stale 1.
    (tmp_path / "bus_meta.json").write_text(json.dumps({"last_seq": 1}))
    comms.send("peer", viewer, "new after rollback")
    assert comms.pending_count(viewer, "peer") == 1
