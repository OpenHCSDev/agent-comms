"""Additional no-provider identity and lock-order probes for scoped DM basis."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_comms.comms import wire
from agent_comms.thread_identity import ThreadRole
from agent_comms.threads import Thread


def _peer(root: Path, name: str) -> Thread:
    return Thread(name, frozenset({"talk"}), str(root), pid=os.getpid())


def _mark(comms, peer, root, page):
    assert page.display_basis is not None and page.newest_seq is not None
    comms.views.mark_dm_view_read(
        peer,
        worktree=str(root),
        through=page.newest_seq,
        expected_display_basis=page.display_basis,
    )


def test_alias_rename_invalidates_old_basis_but_fresh_alias_page_can_mark(tmp_path: Path):
    comms = wire(tmp_path)
    comms.threads.register(_peer(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "before rename")
    old = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    comms.threads.rename_managed_thread("peer", "renamed", owner_pid=os.getpid())
    markers = tmp_path / "read_ledger.json"
    marker_before = markers.read_bytes() if markers.exists() else None
    with pytest.raises(ValueError, match="incarnation changed"):
        _mark(comms, "peer", tmp_path, old)
    assert (markers.read_bytes() if markers.exists() else None) == marker_before
    assert comms.bus.pending_count(viewer, "renamed") == 1

    # The old spelling is now an alias for the SAME incarnation. A fresh
    # page, not stale name text, is authoritative after the rename.
    fresh = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert fresh.display_basis is not None and fresh.display_basis.peer == "renamed"
    _mark(comms, "peer", tmp_path, fresh)
    assert comms.bus.pending_count(viewer, "renamed") == 0
    comms.messaging.send("renamed", viewer, "after bound")
    assert comms.bus.pending_count(viewer, "renamed") == 1


def test_delete_and_same_name_reregister_invalidates_old_peer_basis(tmp_path: Path):
    comms = wire(tmp_path)
    comms.threads.register(_peer(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "old peer painted")
    old = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert old.display_basis is not None
    comms.registry.unregister("peer")
    comms.threads.delete("peer")
    comms.threads.register(_peer(tmp_path, "peer"))
    comms.messaging.send("peer", viewer, "new peer unseen")
    before = comms.bus.pending_count(viewer, "peer")
    markers = tmp_path / "read_ledger.json"
    marker_before = markers.read_bytes() if markers.exists() else None
    assert before == 1
    with pytest.raises(ValueError, match="incarnation changed"):
        _mark(comms, "peer", tmp_path, old)
    assert (markers.read_bytes() if markers.exists() else None) == marker_before
    assert comms.bus.pending_count(viewer, "peer") == 1
    fresh = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert fresh.display_basis is not None
    assert fresh.display_basis.peer_created_at != old.display_basis.peer_created_at


def test_viewer_rebind_and_foreign_worktree_reject_old_basis(tmp_path: Path):
    comms = wire(tmp_path)
    comms.threads.register(_peer(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "old viewer painted")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert page.display_basis is not None
    with pytest.raises(ValueError, match="contiguous"):
        comms.views.mark_dm_view_read(
            "peer",
            worktree=str(tmp_path / "foreign"),
            through=page.newest_seq,
            expected_display_basis=page.display_basis,
        )
    comms.registry.unregister(viewer)
    comms.threads.delete(viewer)
    comms.threads.register(Thread(viewer, frozenset(), str(tmp_path), role=ThreadRole.USER))
    comms.messaging.send("peer", viewer, "new viewer unseen")
    before = comms.bus.pending_count(viewer, "peer")
    assert before == 1
    with pytest.raises(ValueError, match="incarnation changed"):
        _mark(comms, "peer", tmp_path, page)
    assert comms.bus.pending_count(viewer, "peer") == before


def test_old_viewer_alive_but_new_human_selected_rejects_stale_basis(tmp_path: Path):
    comms = wire(tmp_path)
    comms.threads.register(_peer(tmp_path, "peer"))
    old_viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", old_viewer, "old viewer painted")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert page.display_basis is not None and page.display_basis.viewer == old_viewer
    comms.threads.register(Thread("new_user", frozenset(), str(tmp_path), role=ThreadRole.USER))
    # Normal registry rename retains the old human declaration but moves its
    # insertion position behind new_user. user_identity now selects new_user.
    comms.registry.rename(old_viewer, "old_user")
    assert "old_user" in comms.registry
    assert comms.messaging.user_identity(str(tmp_path)).name == "new_user"
    comms.messaging.send("peer", "new_user", "new viewer unseen")
    before = comms.bus.pending_count("new_user", "peer")
    assert before == 1
    marker_path = tmp_path / "read_ledger.json"
    marker_before = marker_path.read_bytes() if marker_path.exists() else None
    with pytest.raises(ValueError, match="registry changed|viewer/peer incarnation changed"):
        _mark(comms, "peer", tmp_path, page)
    assert (marker_path.read_bytes() if marker_path.exists() else None) == marker_before
    assert comms.bus.pending_count("new_user", "peer") == before


def test_marker_changed_during_page_fails_before_basis_issued(tmp_path: Path, monkeypatch):
    comms = wire(tmp_path)
    comms.threads.register(_peer(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "painted")
    original = comms.bus.dm_history_page

    def interpose(*args, **kwargs):
        page = original(*args, **kwargs)
        comms.bus.reads.mark_displayed(
            viewer,
            comms.bus.reads.capture(viewer, page.messages, comms.registry.snapshot(), comms.bus.log.path),
        )
        return page

    monkeypatch.setattr(comms.bus, "dm_history_page", interpose)
    with pytest.raises(ValueError, match="changed while paging"):
        comms.views.dm_display_page("peer", worktree=str(tmp_path))


def test_lifecycle_and_marker_lock_order_does_not_deadlock(tmp_path: Path):
    comms = wire(tmp_path)
    for name in ("peer", "other"):
        comms.threads.register(_peer(tmp_path, name))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "painted")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    with ThreadPoolExecutor(max_workers=2) as pool:
        marked = pool.submit(_mark, comms, "peer", tmp_path, page)
        lifecycle = pool.submit(
            comms.threads.rename_managed_thread, "other", "renamed", owner_pid=os.getpid()
        )
        # Either order is valid; an unrelated registry change may reject the
        # stale basis, but the registry->marker path must not hang.
        try:
            marked.result(timeout=3)
        except ValueError as error:
            assert "registry changed" in str(error)
        lifecycle.result(timeout=3)
    assert comms.bus.pending_count(viewer, "peer") in {0, 1}
    comms.messaging.send("peer", viewer, "after painted boundary")
    assert comms.bus.pending_count(viewer, "peer") >= 1
