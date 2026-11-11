"""Additional no-provider identity and lock-order probes for scoped DM basis."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_comms.messaging.channel_targets import CommandTarget, ThreadTarget
from agent_comms.comms import wire
from agent_comms.coordination.errors import DMScopeChanged
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.messaging.message_page import PageBounds
from agent_comms.projects.checkouts import Checkout
from agent_comms.messaging.read_basis import DMSelection
from agent_comms.registry.thread_identity import UserThreadRole
from agent_comms.registry.threads import NativeThread, Thread
from bus_record_fixture import prepare_native_comms
from conftest import installed_comms


@pytest.fixture(autouse=True)
def installed_bus(tmp_path: Path):
    return prepare_native_comms(wire(tmp_path))


def _peer(root: Path, name: str) -> Thread:
    return NativeThread(name, frozenset({'talk'}), Checkout(root), process_identity=ProcessIdentity.capture(os.getpid()))


def _ledger(root: Path) -> bytes | None:
    """The read ledger's bytes, or None before anything was marked."""
    markers = root / "read_ledger.json"
    return markers.read_bytes() if markers.exists() else None


def _mark(comms, root, page):
    assert isinstance(page.selection, DMSelection) and page.newest_seq is not None
    comms.views.mark_page_read(page, worktree=Checkout(root))


def test_alias_rename_invalidates_old_basis_but_fresh_alias_page_can_mark(tmp_path: Path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(_peer(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(Checkout(tmp_path)).name
    comms.messaging.send("peer", viewer, "before rename")
    old = comms.views.dm_display_page(ThreadTarget("peer"), worktree=Checkout(tmp_path))
    comms.threads.rename_managed_thread(comms.registry.require("peer").incarnation, "renamed", owner=ProcessIdentity.capture(os.getpid()))
    marker_before = _ledger(tmp_path)
    with pytest.raises(DMScopeChanged):
        _mark(comms, tmp_path, old)
    assert _ledger(tmp_path) == marker_before
    assert comms.bus.pending_count(viewer, CommandTarget.named("renamed")) == 1

    # The old spelling is now an alias for the SAME incarnation. A fresh
    # page, not stale name text, is authoritative after the rename.
    fresh = comms.views.dm_display_page(ThreadTarget("peer"), worktree=Checkout(tmp_path))
    assert isinstance(fresh.selection, DMSelection) and fresh.selection.basis.peer == "renamed"
    _mark(comms, tmp_path, fresh)
    assert comms.bus.pending_count(viewer, CommandTarget.named("renamed")) == 0
    comms.messaging.send("renamed", viewer, "after bound")
    assert comms.bus.pending_count(viewer, CommandTarget.named("renamed")) == 1


def test_delete_and_same_name_reregister_invalidates_old_peer_basis(tmp_path: Path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(_peer(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(Checkout(tmp_path)).name
    comms.messaging.send("peer", viewer, "old peer painted")
    old = comms.views.dm_display_page(ThreadTarget("peer"), worktree=Checkout(tmp_path))
    assert isinstance(old.selection, DMSelection)
    comms.registry.unregister("peer")
    comms.registry.remove("peer")
    comms.registry.declare(_peer(tmp_path, "peer"))
    comms.messaging.send("peer", viewer, "new peer unseen")
    before = comms.bus.pending_count(viewer, CommandTarget.named("peer"))
    marker_before = _ledger(tmp_path)
    assert before == 1
    with pytest.raises(DMScopeChanged):
        _mark(comms, tmp_path, old)
    assert _ledger(tmp_path) == marker_before
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 1
    fresh = comms.views.dm_display_page(ThreadTarget("peer"), worktree=Checkout(tmp_path))
    assert isinstance(fresh.selection, DMSelection)
    assert fresh.selection.basis.peer_identity != old.selection.basis.peer_identity


def test_viewer_rebind_and_foreign_worktree_reject_old_basis(tmp_path: Path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(_peer(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(Checkout(tmp_path)).name
    comms.messaging.send("peer", viewer, "old viewer painted")
    page = comms.views.dm_display_page(ThreadTarget("peer"), worktree=Checkout(tmp_path))
    assert isinstance(page.selection, DMSelection)
    with pytest.raises(DMScopeChanged):
        comms.views.mark_page_read(page, worktree=Checkout(tmp_path / "foreign"))
    comms.registry.unregister(viewer)
    comms.registry.remove(viewer)
    comms.registry.declare(NativeThread(viewer, frozenset(), Checkout(tmp_path), role=UserThreadRole))
    comms.messaging.send("peer", viewer, "new viewer unseen")
    before = comms.bus.pending_count(viewer, CommandTarget.named("peer"))
    assert before == 1
    with pytest.raises(DMScopeChanged):
        _mark(comms, tmp_path, page)
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == before


def test_old_viewer_alive_but_new_human_selected_rejects_stale_basis(tmp_path: Path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(_peer(tmp_path, "peer"))
    old_viewer = comms.messaging.user_identity(Checkout(tmp_path)).name
    comms.messaging.send("peer", old_viewer, "old viewer painted")
    page = comms.views.dm_display_page(ThreadTarget("peer"), worktree=Checkout(tmp_path))
    assert isinstance(page.selection, DMSelection) and page.selection.basis.viewer == old_viewer
    comms.registry.declare(NativeThread('new_user', frozenset(), Checkout(tmp_path), role=UserThreadRole))
    # Normal registry rename retains the old human declaration but moves its
    # insertion position behind new_user. user_identity now selects new_user.
    comms.registry.rename(old_viewer, "old_user")
    assert "old_user" in comms.registry
    assert comms.messaging.user_identity(Checkout(tmp_path)).name == "new_user"
    comms.messaging.send("peer", "new_user", "new viewer unseen")
    before = comms.bus.pending_count("new_user", CommandTarget.named("peer"))
    assert before == 1
    marker_before = _ledger(tmp_path)
    with pytest.raises(DMScopeChanged):
        _mark(comms, tmp_path, page)
    assert _ledger(tmp_path) == marker_before
    assert comms.bus.pending_count("new_user", CommandTarget.named("peer")) == before


def test_newest_page_mark_leaves_older_message_unread(tmp_path: Path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(_peer(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(Checkout(tmp_path)).name
    older = comms.messaging.send("peer", viewer, "older").message
    newest = comms.messaging.send("peer", viewer, "newest").message
    page = comms.views.dm_display_page(ThreadTarget("peer"), worktree=Checkout(tmp_path), bounds=PageBounds(limit=1))
    assert page.messages == (newest,)
    _mark(comms, tmp_path, page)
    assert comms.bus.inbox(viewer, CommandTarget.named("peer")).page.messages == (older,)


def test_lifecycle_and_marker_lock_order_does_not_deadlock(tmp_path: Path):
    comms = installed_comms(tmp_path)
    for name in ("peer", "other"):
        comms.registry.declare(_peer(tmp_path, name))
    viewer = comms.messaging.user_identity(Checkout(tmp_path)).name
    comms.messaging.send("peer", viewer, "painted")
    page = comms.views.dm_display_page(ThreadTarget("peer"), worktree=Checkout(tmp_path))
    with ThreadPoolExecutor(max_workers=2) as pool:
        marked = pool.submit(_mark, comms, tmp_path, page)
        lifecycle = pool.submit(
            comms.threads.rename_managed_thread, comms.registry.require("other").incarnation, "renamed", owner=ProcessIdentity.capture(os.getpid())
        )
        # Either order is valid; an unrelated registry change may reject the
        # stale basis, but the registry->marker path must not hang.
        try:
            marked.result(timeout=3)
        except DMScopeChanged:
            pass
        lifecycle.result(timeout=3)
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) in {0, 1}
    comms.messaging.send("peer", viewer, "after painted boundary")
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) >= 1
