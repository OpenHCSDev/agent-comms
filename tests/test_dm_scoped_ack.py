"""No-provider bounded human DM read evidence; never use global inbox ACK."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_comms import locked_store
from agent_comms.comms import wire
from agent_comms.read_basis import DMDisplayBasis
from agent_comms.threads import Thread


def _thread(root: Path, name: str) -> Thread:
    return Thread(name, frozenset({"talk"}), str(root), pid=os.getpid())


def test_dm_page_requires_deliberate_baseline_before_omitted_older_unread(tmp_path: Path):
    comms = wire(tmp_path)
    comms.threads.register(_thread(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    for index in range(50):
        comms.messaging.send("peer", viewer, f"old {index}")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path), limit=5)
    assert len(page.messages) == 5 and page.has_older
    assert page.display_basis is not None and page.display_basis.older_unread
    before = (
        (tmp_path / "read_ledger.json").read_bytes()
        if (tmp_path / "read_ledger.json").exists()
        else None
    )
    with pytest.raises(ValueError, match="contiguous"):
        comms.views.mark_dm_view_read(
            "peer",
            worktree=str(tmp_path),
            through=page.newest_seq,
            expected_display_basis=page.display_basis,
        )
    after = (
        (tmp_path / "read_ledger.json").read_bytes()
        if (tmp_path / "read_ledger.json").exists()
        else None
    )
    assert before == after
    assert comms.bus.pending_count(viewer, "peer") == 50


def test_deliberate_baseline_and_painted_page_ack_only_peer_through_bound(tmp_path: Path):
    comms = wire(tmp_path)
    for name in ("peer", "other", "executor"):
        comms.threads.register(_thread(tmp_path, name))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    for index in range(50):
        comms.messaging.send("peer", viewer, f"old {index}")
    comms.views.mark_user_view_read("peer", worktree=str(tmp_path))
    assert comms.bus.pending_count(viewer, "peer") == 0
    comms.messaging.send("peer", viewer, "painted")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path), limit=5)
    proof = page.display_basis
    assert isinstance(proof, DMDisplayBasis) and not proof.older_unread
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
    assert [message.body for message in reopened.bus.inbox(viewer, "peer")] == ["after paint"]
    assert reopened.bus.pending_count(viewer, "peer") == 1
    assert reopened.bus.pending_count(viewer, "other") == 1
    assert reopened.bus.pending_count("executor", "peer") == 1
    assert reopened.bus.pending_count("executor", "#all") == 1
    # No global marker is created by this human DM read transition.
    seen = reopened.bus.reads.seen_sequences(viewer, reopened.registry.snapshot())
    assert painted in seen and painted + 1 not in seen
    assert not (tmp_path / "read_markers.json").exists()


def test_peer_delete_same_name_rebind_rejects_stale_page_without_read_ack(tmp_path: Path):
    comms = wire(tmp_path)
    for name in ("peer", "other"):
        comms.threads.register(_thread(tmp_path, name))
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
    before = comms.bus.pending_count(viewer, "peer")
    marker_path = tmp_path / "read_ledger.json"
    markers_before = marker_path.read_bytes() if marker_path.exists() else None
    assert before == 3
    with pytest.raises(ValueError, match="registry changed|incarnation changed"):
        comms.views.mark_dm_view_read(
            "peer",
            worktree=str(tmp_path),
            through=5,
            expected_display_basis=page.display_basis,
        )
    markers_after = marker_path.read_bytes() if marker_path.exists() else None
    assert markers_after == markers_before
    assert comms.bus.pending_count(viewer, "peer") == before


def test_foreign_root_wrong_peer_and_unbounded_or_bool_through_rejected(tmp_path: Path):
    first, second = wire(tmp_path / "first"), wire(tmp_path / "second")
    for comms in (first, second):
        for name in ("peer", "other"):
            comms.threads.register(_thread(comms.root, name))
        viewer = comms.messaging.user_identity(str(comms.root)).name
        comms.messaging.send("peer", viewer, "painted")
    page = first.views.dm_display_page("peer", worktree=str(first.root))
    assert page.display_basis is not None and page.newest_seq is not None
    for comms, target, through in (
        (second, "peer", page.newest_seq),
        (first, "other", page.newest_seq),
        (first, "peer", page.newest_seq + 1),
        (first, "peer", True),
        (first, "peer", -1),
    ):
        with pytest.raises(ValueError):
            comms.views.mark_dm_view_read(
                target,
                worktree=str(comms.root),
                through=through,
                expected_display_basis=page.display_basis,
            )
    assert first.bus.pending_count(first.messaging.user_identity(str(first.root)).name, "peer") == 1


def test_independent_read_is_idempotent_and_never_retargets_page(tmp_path: Path):
    comms = wire(tmp_path)
    comms.threads.register(_thread(tmp_path, "peer"))
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
    assert comms.bus.pending_count(viewer, "peer") == 1


@pytest.fixture
def durable_root():
    with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
        yield Path(directory)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory fsync")
def test_scoped_marker_fsyncs_parent_and_sync_denial_is_not_success(durable_root, monkeypatch):
    tmp_path = durable_root
    comms = wire(tmp_path)
    comms.threads.register(_thread(tmp_path, "peer"))
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
    assert parent_fd_seen and comms.bus.pending_count(viewer, "peer") == 0

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
    assert comms.bus.pending_count(viewer, "peer") == 1
    # A failure at the *final* post-replace sync remains UNKNOWN: absent a
    # separate durable marker commit witness, the row may already be visible.


