"""Additional no-provider identity and lock-order probes for scoped DM basis."""

from __future__ import annotations
from agent_comms.channel_targets import CommandTarget
from agent_comms.registration import Registration

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.coordination_errors import DMScopeChanged
from agent_comms.message_page import ConversationMessagePageRequest, MessagePageRequest, PageBounds
from agent_comms.store_files import _store_lock
from agent_comms.thread_identity import UserThreadRole
from agent_comms.threads import Thread


def _peer(root: Path, name: str) -> Thread:
    thread_registration = Registration(Path(root) / "registry.json")
    return thread_registration.new_thread(name, frozenset({'talk'}), str(root), process_identity=ProcessIdentity.capture(os.getpid()))


def _mark(comms, peer, root, page):
    assert page.display_basis is not None and page.newest_seq is not None
    comms.views.mark_dm_view_read(
        peer,
        worktree=str(root),
        through=page.newest_seq,
        expected_display_basis=page.display_basis,
    )


def test_dm_preparation_releases_native_claim_lock(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(_peer(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    for index in range(3):
        comms.messaging.send("peer", viewer, f"original {index}")
    read = MessagePageRequest.read
    prepared = []

    def inspect(request, log):
        # An independent descriptor must acquire the real claim_scope lock while
        # the original reader still prepares the page; no replacement reader.
        with _store_lock(tmp_path / "wire", blocking=False):
            prepared.append(True)
        page = read(request, log)
        comms.messaging.send("peer", viewer, "arrived after opened page")
        return page

    monkeypatch.setattr(MessagePageRequest, "read", inspect)
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path), bounds=PageBounds(limit=1))
    assert prepared == [True]
    assert page.messages[0].body == "original 2"
    assert page.has_older
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 4


def test_alias_rename_invalidates_old_basis_but_fresh_alias_page_can_mark(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_peer(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "before rename")
    old = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    comms.threads.rename_managed_thread("peer", "renamed", owner_pid=os.getpid())
    markers = tmp_path / "read_ledger.json"
    marker_before = markers.read_bytes() if markers.exists() else None
    with pytest.raises(DMScopeChanged, match="incarnation changed"):
        _mark(comms, "peer", tmp_path, old)
    assert (markers.read_bytes() if markers.exists() else None) == marker_before
    assert comms.bus.pending_count(viewer, CommandTarget.named("renamed")) == 1

    # The old spelling is now an alias for the SAME incarnation. A fresh
    # page, not stale name text, is authoritative after the rename.
    fresh = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert fresh.display_basis is not None and fresh.display_basis.peer == "renamed"
    _mark(comms, "peer", tmp_path, fresh)
    assert comms.bus.pending_count(viewer, CommandTarget.named("renamed")) == 0
    comms.messaging.send("renamed", viewer, "after bound")
    assert comms.bus.pending_count(viewer, CommandTarget.named("renamed")) == 1


def test_delete_and_same_name_reregister_invalidates_old_peer_basis(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_peer(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "old peer painted")
    old = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert old.display_basis is not None
    comms.registry.unregister("peer")
    comms.registry.remove("peer")
    comms.registry.declare(_peer(tmp_path, "peer"))
    comms.messaging.send("peer", viewer, "new peer unseen")
    before = comms.bus.pending_count(viewer, CommandTarget.named("peer"))
    markers = tmp_path / "read_ledger.json"
    marker_before = markers.read_bytes() if markers.exists() else None
    assert before == 1
    with pytest.raises(DMScopeChanged, match="incarnation changed"):
        _mark(comms, "peer", tmp_path, old)
    assert (markers.read_bytes() if markers.exists() else None) == marker_before
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == 1
    fresh = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert fresh.display_basis is not None
    assert fresh.display_basis.peer_created_at != old.display_basis.peer_created_at


def test_viewer_rebind_and_foreign_worktree_reject_old_basis(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_peer(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", viewer, "old viewer painted")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert page.display_basis is not None
    with pytest.raises(DMScopeChanged, match="different root, worktree or peer"):
        comms.views.mark_dm_view_read(
            "peer",
            worktree=str(tmp_path / "foreign"),
            through=page.newest_seq,
            expected_display_basis=page.display_basis,
        )
    comms.registry.unregister(viewer)
    comms.registry.remove(viewer)
    comms.registry.declare(comms.registry.new_thread(viewer, frozenset(), str(tmp_path), role=UserThreadRole))
    comms.messaging.send("peer", viewer, "new viewer unseen")
    before = comms.bus.pending_count(viewer, CommandTarget.named("peer"))
    assert before == 1
    with pytest.raises(DMScopeChanged, match="incarnation changed"):
        _mark(comms, "peer", tmp_path, page)
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) == before


def test_old_viewer_alive_but_new_human_selected_rejects_stale_basis(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_peer(tmp_path, "peer"))
    old_viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("peer", old_viewer, "old viewer painted")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert page.display_basis is not None and page.display_basis.viewer == old_viewer
    comms.registry.declare(comms.registry.new_thread('new_user', frozenset(), str(tmp_path), role=UserThreadRole))
    # Normal registry rename retains the old human declaration but moves its
    # insertion position behind new_user. user_identity now selects new_user.
    comms.registry.rename(old_viewer, "old_user")
    assert "old_user" in comms.registry
    assert comms.messaging.user_identity(str(tmp_path)).name == "new_user"
    comms.messaging.send("peer", "new_user", "new viewer unseen")
    before = comms.bus.pending_count("new_user", CommandTarget.named("peer"))
    assert before == 1
    marker_path = tmp_path / "read_ledger.json"
    marker_before = marker_path.read_bytes() if marker_path.exists() else None
    with pytest.raises(DMScopeChanged, match="registry changed|viewer/peer incarnation changed"):
        _mark(comms, "peer", tmp_path, page)
    assert (marker_path.read_bytes() if marker_path.exists() else None) == marker_before
    assert comms.bus.pending_count("new_user", CommandTarget.named("peer")) == before


@pytest.mark.parametrize("marked_sender", ("peer", "other"))
def test_read_progress_during_fetch_does_not_invalidate_page(
    tmp_path: Path, monkeypatch, marked_sender,
):
    comms = wire(tmp_path)
    for name in ("peer", "other"):
        comms.registry.declare(_peer(tmp_path, name))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    messages = {
        name: comms.messaging.send_message(name, viewer, name) for name in ("peer", "other")
    }
    original = ConversationMessagePageRequest.read

    def interpose(request, log):
        page = original(request, log)
        comms.bus.reads.mark_displayed(
            viewer,
            comms.bus.reads.capture(
                viewer, (messages[marked_sender],), comms.registry.snapshot(), comms.bus.log.path,
            ),
        )
        return page

    monkeypatch.setattr(ConversationMessagePageRequest, "read", interpose)
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path))
    assert page.messages == (messages["peer"],)
    assert page.display_basis is not None
    _mark(comms, "peer", tmp_path, page)
    cursor = comms.bus.reads.cursor(viewer, comms.registry.snapshot())
    expected = {messages["peer"].seq, messages[marked_sender].seq}
    assert len(cursor) == len(expected)
    assert all(sequence in cursor for sequence in expected)


def test_newest_page_mark_leaves_older_message_unread(tmp_path: Path):
    comms = wire(tmp_path)
    comms.registry.declare(_peer(tmp_path, "peer"))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    older = comms.messaging.send_message("peer", viewer, "older")
    newest = comms.messaging.send_message("peer", viewer, "newest")
    page = comms.views.dm_display_page("peer", worktree=str(tmp_path), bounds=PageBounds(limit=1))
    assert page.messages == (newest,)
    _mark(comms, "peer", tmp_path, page)
    assert comms.bus.inbox(viewer, CommandTarget.named("peer")) == [older]


def test_lifecycle_and_marker_lock_order_does_not_deadlock(tmp_path: Path):
    comms = wire(tmp_path)
    for name in ("peer", "other"):
        comms.registry.declare(_peer(tmp_path, name))
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
        except DMScopeChanged as error:
            assert "registry changed" in str(error)
        lifecycle.result(timeout=3)
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) in {0, 1}
    comms.messaging.send("peer", viewer, "after painted boundary")
    assert comms.bus.pending_count(viewer, CommandTarget.named("peer")) >= 1
