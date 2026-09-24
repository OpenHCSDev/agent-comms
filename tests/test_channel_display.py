"""The opt-in channel activity feed is a local display, never wire authority."""

import json
from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from threading import Thread as WorkerThread
from unittest.mock import patch

import pytest

from agent_comms import (
    Message,
    MessageType,
    SavedView,
    Thread,
    ViewKind,
    ViewMatch,
    ViewPredicate,
    wire,
)
from agent_comms import channels as channel_module


def populated(tmp_path):
    comms = wire(tmp_path)
    for name, tags in (
        ("alice", {"api"}),
        ("carol", {"api"}),
        ("bob", {"ui"}),
        ("outsider", set()),
    ):
        comms.register(Thread(name, frozenset(tags), str(tmp_path)))
    return comms


def bodies(page):
    return [message.body for message in page.messages]


def test_default_off_and_opt_in_only_changes_display(tmp_path):
    comms = populated(tmp_path)
    comms.send("outsider", "#api", "exact target")
    comms.send("alice", "bob", "member to outsider")
    comms.send("bob", "carol", "outsider to member")
    comms.send("outsider", "bob", "explicit @alice mention")
    comms.send("outsider", "#ui", "unrelated channel")
    comms.send("outsider", "bob", "ordinary outsider DM")
    before_delivery = comms.pending_counts("alice")
    before_wire = tmp_path.joinpath("bus.jsonl").read_bytes()
    assert bodies(comms.channel_history_page("#api")) == ["exact target"]
    assert bodies(comms.channel_display_page("#api")) == ["exact target"]

    selected = comms.set_channel_any_mode("#api", True)
    assert selected.exact and selected.any_mode
    assert wire(tmp_path).channel_catalog.resolve("#api").any_mode
    page = comms.channel_display_page("#api")
    assert bodies(page) == [
        "exact target",
        "member to outsider",
        "outsider to member",
        "explicit @alice mention",
    ]
    assert [message.target for message in page.messages] == ["#api", "bob", "carol", "bob"]
    assert len({message.seq for message in page.messages}) == len(page.messages)
    assert bodies(comms.channel_history_page("#api")) == ["exact target"]
    assert [message.body for message in comms.channel_history("#api")] == ["exact target"]
    assert bodies(comms.channel_display_page("#ui")) == ["unrelated channel"]
    assert tmp_path.joinpath("bus.jsonl").read_bytes() == before_wire
    assert comms.pending_counts("alice") == before_delivery
    assert not comms.set_channel_any_mode("api", False).any_mode
    assert bodies(comms.channel_display_page("#api")) == ["exact target"]


def test_mode_rejects_builtins_union_view_and_unknown_without_sidecar_mutation(tmp_path):
    comms = populated(tmp_path)
    comms.set_channel("engineering", frozenset({"api", "ui"}))
    comms.set_saved_view(
        SavedView("saved", ViewKind.ACTIVITY, ViewPredicate(ViewMatch.ANY_OF, frozenset({"api"})))
    )
    metadata = comms.channel_catalog.metadata_path
    before = metadata.read_bytes() if metadata.exists() else None
    for channel in ("#any", "#all", "#none", "#engineering", "#saved", "#missing"):
        with pytest.raises(ValueError, match="Only existing exact tag channels"):
            comms.set_channel_any_mode(channel, True)
    with pytest.raises(ValueError, match="boolean"):
        comms.set_channel_any_mode("#api", 1)
    assert (metadata.read_bytes() if metadata.exists() else None) == before
    with pytest.raises(ValueError, match="Unknown channel"):
        comms.channel_display_page("#missing")


def test_single_captured_basis_survives_mode_change_during_snapshot(tmp_path):
    comms = populated(tmp_path)
    comms.send("alice", "bob", "member DM")
    original = comms.bus._record_snapshot
    started, finished = Event(), Event()

    def toggle():
        started.set()
        comms.set_channel_any_mode("#api", True)
        finished.set()

    @contextmanager
    def toggle_after_boundary(*, need_sequence=True):
        with original(need_sequence=need_sequence) as snapshot:
            writer = WorkerThread(target=toggle, daemon=True)
            writer.start()  # blocked on the short wire lock until the boundary opens
            assert started.wait(timeout=3) and not finished.is_set()
            try:
                yield snapshot
            finally:
                writer.join(timeout=3)
                assert finished.is_set()

    with patch.object(comms.bus, "_record_snapshot", side_effect=toggle_after_boundary):
        first = comms.viewer_snapshot(str(tmp_path))
    old = next(view for view in first.channels if view.channel.name == "#api")
    assert not old.channel.any_mode
    assert old.last_activity == 0
    assert first.channel_unread["#api"] == 0

    second = comms.viewer_snapshot(str(tmp_path))
    new = next(view for view in second.channels if view.channel.name == "#api")
    assert new.channel.any_mode
    assert new.last_activity > 0
    assert second.channel_unread["#api"] == 1
    assert comms.message_high_water() == 1
    assert len(comms.channel_display_page("#any").messages) == 1


def test_opt_in_user_input_activity_uses_same_member_scope(tmp_path):
    comms = populated(tmp_path)
    comms.send_user_message("alice", "human input to member", worktree=str(tmp_path))
    off = next(
        view
        for view in comms.viewer_snapshot(str(tmp_path)).channels
        if view.channel.name == "#api"
    )
    assert off.last_user_input == 0
    comms.set_channel_any_mode("#api", True)
    on = next(
        view
        for view in comms.viewer_snapshot(str(tmp_path)).channels
        if view.channel.name == "#api"
    )
    assert on.last_user_input > 0
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 0
    assert bodies(comms.channel_display_page("#api")) == ["human input to member"]
    assert bodies(comms.channel_history_page("#api")) == []


def test_alias_retag_and_tag_rename_reproject_without_new_wire_row(tmp_path):
    comms = populated(tmp_path)
    comms.send("alice", "bob", "old sender DM")
    comms.set_channel_any_mode("api", True)
    high_water = comms.message_high_water()
    assert bodies(comms.channel_display_page("#api")) == ["old sender DM"]
    initial = {view.channel.name: view for view in comms.viewer_snapshot(str(tmp_path)).channels}
    assert initial["#api"].last_activity > 0

    comms.registry.rename("alice", "renamed")
    assert comms.message_high_water() == high_water
    assert bodies(comms.channel_display_page("#api")) == ["old sender DM"]
    renamed = comms.registry.require("renamed")
    comms.registry.register(
        replace(renamed, tags=frozenset({"ui"})), comms.registry.status("renamed")
    )
    assert comms.message_high_water() == high_water
    assert bodies(comms.channel_display_page("#api")) == []
    after = {view.channel.name: view for view in comms.viewer_snapshot(str(tmp_path)).channels}
    assert after["#api"].last_activity < initial["#api"].last_activity

    comms.registry.register(replace(comms.registry.require("renamed"), tags=frozenset({"api"})))
    comms.rename_tag("api", "services")
    assert comms.message_high_water() == high_water
    assert comms.channel_catalog.resolve("#services").any_mode
    assert not comms.channel_catalog.resolve("#api").any_mode
    assert bodies(comms.channel_display_page("#services")) == ["old sender DM"]
    comms.delete_tag("services")
    metadata = comms.channel_catalog.metadata_path
    assert not any(
        value.get("any_mode", False)
        for value in json.loads(metadata.read_text()).get("channels", {}).values()
    )


def test_human_marker_unread_and_own_alias_exclusion(tmp_path):
    comms = populated(tmp_path)
    viewer = comms.user_identity(str(tmp_path)).name
    comms.send_user_message("#api", "owned before rename", worktree=str(tmp_path))
    comms.registry.rename(viewer, "human")
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 0
    comms.send("alice", "bob", "before threshold")
    comms.mark_channel_view_read("#api", worktree=str(tmp_path))
    comms.set_channel_any_mode("#api", True)
    # The previously hidden DM was never painted, so enabling the broader
    # projection must expose it as unread despite the exact-channel marker.
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 1
    comms.send("bob", "alice", "after threshold")
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 2
    # The old user sender is still an alias of the current human viewer.
    comms.send_user_message("#api", "human owned", worktree=str(tmp_path))
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 2
    comms.mark_channel_view_read("#api", worktree=str(tmp_path))
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 0


def test_page_boundaries_byte_budget_and_truncated_tail_match_target_pager(tmp_path):
    comms = populated(tmp_path)
    comms.send("alice", "bob", "first projected")
    comms.send("outsider", "#api", "second target")
    comms.send("bob", "alice", "third projected")
    comms.set_channel_any_mode("#api", True)
    rows = comms.channel_display_page("#api").messages
    first_bytes = len((json.dumps(rows[0].to_wire()) + "\n").encode())
    assert bodies(comms.channel_display_page("#api", after=0, limit=20, max_bytes=first_bytes)) == [
        "first projected"
    ]
    head = comms.channel_display_page("#api", after=0, limit=1)
    assert head.has_newer and not head.has_older
    tail = comms.channel_display_page("#api", before=rows[-1].seq, limit=1)
    assert bodies(tail) == ["second target"] and tail.has_older and tail.has_newer
    with pytest.raises(ValueError, match="either before or after"):
        comms.channel_display_page("#api", before=2, after=1)
    with (tmp_path / "bus.jsonl").open("ab") as stream:
        stream.write(b'{"seq":')
    assert bodies(comms.channel_display_page("#api")) == [
        "first projected",
        "second target",
        "third projected",
    ]
    assert bodies(comms.channel_history_page("#api")) == ["second target"]


def test_fixed_opened_boundary_excludes_later_append(tmp_path):
    comms = populated(tmp_path)
    comms.send("alice", "bob", "within boundary")
    comms.set_channel_any_mode("#api", True)
    original = comms.bus._record_snapshot
    started, finished = Event(), Event()

    def append():
        started.set()
        comms.send("alice", "bob", "outside boundary")
        finished.set()

    @contextmanager
    def with_append(*, need_sequence=True):
        with original(need_sequence=need_sequence) as snapshot:
            writer = WorkerThread(target=append, daemon=True)
            writer.start()
            assert started.wait(timeout=3) and not finished.is_set()
            try:
                yield snapshot
            finally:
                writer.join(timeout=3)
                assert finished.is_set()

    with patch.object(comms.bus, "_record_snapshot", side_effect=with_append):
        assert bodies(comms.channel_display_page("#api")) == ["within boundary"]
    assert bodies(comms.channel_display_page("#api")) == ["within boundary", "outside boundary"]


def test_failed_sidecar_write_does_not_leave_in_memory_mode(tmp_path):
    comms = populated(tmp_path)
    real_write = channel_module._atomic_write_text

    def fail_metadata(path, text):
        if path == comms.channel_catalog.metadata_path:
            raise OSError("synthetic sidecar failure")
        return real_write(path, text)

    with (
        patch.object(channel_module, "_atomic_write_text", side_effect=fail_metadata),
        pytest.raises(OSError, match="sidecar failure"),
    ):
        comms.set_channel_any_mode("#api", True)
    assert not comms.channel_catalog.resolve("#api").any_mode
    assert not wire(tmp_path).channel_catalog.resolve("#api").any_mode
    comms.set_channel_any_mode("#api", True)
    comms.create_tag("later")
    assert wire(tmp_path).channel_catalog.resolve("#api").any_mode


def test_public_retag_and_send_cannot_leak_new_nonmember_into_page(tmp_path):
    comms = populated(tmp_path)
    comms.set_channel_any_mode("#api", True)
    comms.send("alice", "bob", "before retag")
    original = comms.bus._record_snapshot
    started, finished = Event(), Event()

    def retag_and_send():
        started.set()
        comms.update_tags("alice", remove=frozenset({"api"}), add=frozenset({"ui"}))
        comms.send("alice", "bob", "late nonmember")
        finished.set()

    @contextmanager
    def concurrent_public_change(*, need_sequence=True):
        with original(need_sequence=need_sequence) as snapshot:
            writer = WorkerThread(target=retag_and_send, daemon=True)
            writer.start()
            assert started.wait(timeout=3) and not finished.is_set()
            try:
                yield snapshot
            finally:
                writer.join(timeout=3)
                assert finished.is_set()

    with patch.object(comms.bus, "_record_snapshot", side_effect=concurrent_public_change):
        assert bodies(comms.channel_display_page("#api")) == ["before retag"]
    later = comms.channel_display_page("#api")
    assert "late nonmember" not in bodies(later)
    assert all(message.target == "#api" for message in later.messages)
    assert any(message.membership is not None for message in later.messages)


def test_direct_retag_and_send_between_basis_and_open_retries(tmp_path):
    comms = populated(tmp_path)
    comms.set_channel_any_mode("#api", True)
    original = comms.bus._record_snapshot
    changed = False

    @contextmanager
    def mutate_before_open(*, need_sequence=True):
        nonlocal changed
        if not changed:
            changed = True
            alice = comms.registry.require("alice")
            comms.registry.register(replace(alice, tags=frozenset({"ui"})))
            comms.bus.publish(Message("alice", "bob", "late direct", MessageType.INFO))
        with original(need_sequence=need_sequence) as snapshot:
            yield snapshot

    with patch.object(comms.bus, "_record_snapshot", side_effect=mutate_before_open):
        assert bodies(comms.channel_display_page("#api")) == []
    assert changed
    assert bodies(comms.channel_display_page("#api")) == []
    assert bodies(comms.channel_display_page("#any")) == ["late direct"]


def test_direct_rename_and_own_send_do_not_inflate_unread(tmp_path):
    comms = populated(tmp_path)
    viewer = comms.user_identity(str(tmp_path)).name
    comms.send("bob", "#api", "outside unread")
    original = comms.bus._record_snapshot
    changed = False

    @contextmanager
    def rename_and_send_before_open(*, need_sequence=True):
        nonlocal changed
        if not changed:
            changed = True
            comms.registry.rename(viewer, "human")
            comms.bus.publish(Message("human", "#api", "owned unread", MessageType.INFO))
        with original(need_sequence=need_sequence) as snapshot:
            yield snapshot

    with patch.object(comms.bus, "_record_snapshot", side_effect=rename_and_send_before_open):
        raced = comms.viewer_snapshot(str(tmp_path))
    assert changed
    assert raced.channel_unread["#api"] == 1
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 1


def test_public_mark_read_and_append_share_one_viewer_boundary(tmp_path):
    comms = populated(tmp_path)
    comms.send("bob", "#api", "row1")
    original = comms.bus._record_snapshot
    started, finished = Event(), Event()

    def mark_then_append():
        started.set()
        comms.mark_channel_view_read("#api", worktree=str(tmp_path))
        comms.send("bob", "#api", "row2")
        finished.set()

    @contextmanager
    def concurrent_public_mark(*, need_sequence=True):
        with original(need_sequence=need_sequence) as snapshot:
            writer = WorkerThread(target=mark_then_append, daemon=True)
            writer.start()
            assert started.wait(timeout=3) and not finished.is_set()
            try:
                yield snapshot
            finally:
                writer.join(timeout=3)
                assert finished.is_set()

    with patch.object(comms.bus, "_record_snapshot", side_effect=concurrent_public_mark):
        assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 1
    assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 1


def test_perpetual_direct_mutation_fails_closed_after_bounded_retries(tmp_path):
    comms = populated(tmp_path)
    original = comms.bus._record_snapshot
    attempts = 0

    @contextmanager
    def continually_retag(*, need_sequence=True):
        nonlocal attempts
        attempts += 1
        alice = comms.registry.require("alice")
        tags = frozenset({"ui"}) if "api" in alice.tags else frozenset({"api"})
        comms.registry.register(replace(alice, tags=tags))
        with original(need_sequence=need_sequence) as snapshot:
            yield snapshot

    with (
        patch.object(comms.bus, "_record_snapshot", side_effect=continually_retag),
        pytest.raises(RuntimeError, match="Display scope changed during snapshot"),
    ):
        comms.channel_display_page("#api")
    assert attempts == 3


def test_display_open_never_scans_missing_or_zero_bus_meta_under_wire_lock(tmp_path):
    comms = populated(tmp_path)
    comms.send("outsider", "#api", "existing row")
    metadata = tmp_path / "bus_meta.json"
    for body in (None, '{"last_seq": 0}'):
        if body is None:
            metadata.unlink()
        else:
            metadata.write_text(body)
        with patch.object(
            comms.bus,
            "_max_sequence_unlocked",
            side_effect=AssertionError("display must not scan for a sequence watermark"),
        ):
            assert bodies(comms.channel_display_page("#api")) == ["existing row"]
            assert comms.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 1
    with comms.bus.full_history_snapshot() as (through, records):
        assert through == 1  # default export boundary still repairs the missing watermark
        assert [message.body for message in records] == ["existing row"]


def test_new_channel_during_boundary_is_not_falsely_unknown(tmp_path):
    comms = populated(tmp_path)
    original = comms.bus._record_snapshot
    added = False

    @contextmanager
    def register_before_open(*, need_sequence=True):
        nonlocal added
        if not added:
            added = True
            comms.registry.register(Thread("late", frozenset({"new"}), str(tmp_path)))
        with original(need_sequence=need_sequence) as snapshot:
            yield snapshot

    with patch.object(comms.bus, "_record_snapshot", side_effect=register_before_open):
        assert bodies(comms.channel_display_page("#new")) == []
    assert added
