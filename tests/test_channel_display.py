"""The opt-in channel activity feed is a local display, never wire authority."""

import json
from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from threading import Thread as WorkerThread
from unittest.mock import patch

import pytest

from agent_comms.bus_publication import HumanOrigin
from agent_comms.channels import AnyOfMatch, SavedView, ViewKind, ViewPredicate
from agent_comms.comms import wire
from agent_comms.messages import Message, MessageType
from agent_comms.threads import Thread


def populated(tmp_path):
    comms = wire(tmp_path)
    for name, tags in (
        ("alice", {"api"}),
        ("carol", {"api"}),
        ("bob", {"ui"}),
        ("outsider", set()),
    ):
        comms.registry.declare(Thread(name, frozenset(tags), str(tmp_path)))
    return comms


def bodies(page):
    return [message.body for message in page.messages]


def test_default_off_and_opt_in_only_changes_display(tmp_path):
    comms = populated(tmp_path)
    comms.messaging.send("outsider", "#api", "exact target")
    comms.messaging.send("alice", "bob", "member to outsider")
    comms.messaging.send("bob", "carol", "outsider to member")
    comms.messaging.send("outsider", "bob", "explicit @alice mention")
    comms.messaging.send("outsider", "#ui", "unrelated channel")
    comms.messaging.send("outsider", "bob", "ordinary outsider DM")
    before_delivery = comms.bus.pending_counts("alice")
    before_wire = tmp_path.joinpath("bus.jsonl").read_bytes()
    assert bodies(comms.views.channel_history_page("#api")) == ["exact target"]
    assert bodies(comms.views.channel_display_page("#api")) == ["exact target"]

    selected = comms.channels.set_channel_any_mode("#api", True)
    assert selected.exact and selected.any_mode
    assert wire(tmp_path).channels.catalog.read().resolve("#api").any_mode
    page = comms.views.channel_display_page("#api")
    assert bodies(page) == [
        "exact target",
        "member to outsider",
        "outsider to member",
        "explicit @alice mention",
    ]
    assert [message.target for message in page.messages] == ["#api", "bob", "carol", "bob"]
    assert len({message.seq for message in page.messages}) == len(page.messages)
    assert bodies(comms.views.channel_history_page("#api")) == ["exact target"]
    assert [message.body for message in comms.views.channel_history("#api")] == ["exact target"]
    assert bodies(comms.views.channel_display_page("#ui")) == ["unrelated channel"]
    assert tmp_path.joinpath("bus.jsonl").read_bytes() == before_wire
    assert comms.bus.pending_counts("alice") == before_delivery
    assert not comms.channels.set_channel_any_mode("api", False).any_mode
    assert bodies(comms.views.channel_display_page("#api")) == ["exact target"]


def test_mode_rejects_builtins_union_view_and_unknown_without_sidecar_mutation(tmp_path):
    comms = populated(tmp_path)
    comms.channels.set_saved_view(
        SavedView(
            "engineering", ViewKind.ACTIVITY, ViewPredicate(AnyOfMatch, frozenset({"api", "ui"}))
        )
    )
    comms.channels.set_saved_view(
        SavedView("saved", ViewKind.ACTIVITY, ViewPredicate(AnyOfMatch, frozenset({"api"})))
    )
    metadata = comms.channels.catalog.path
    before = metadata.read_bytes() if metadata.exists() else None
    for channel in ("#any", "#all", "#none", "#engineering", "#saved", "#missing"):
        with pytest.raises(ValueError, match="Only existing exact tag channels"):
            comms.channels.set_channel_any_mode(channel, True)
    with pytest.raises(ValueError, match="boolean"):
        comms.channels.set_channel_any_mode("#api", 1)
    assert (metadata.read_bytes() if metadata.exists() else None) == before
    with pytest.raises(ValueError, match="Unknown channel"):
        comms.views.channel_display_page("#missing")


def test_single_captured_basis_survives_mode_change_during_snapshot(tmp_path):
    comms = populated(tmp_path)
    comms.messaging.send("alice", "bob", "member DM")
    original = comms.bus.log._opened_wire_snapshot
    started, finished = Event(), Event()

    def toggle():
        started.set()
        comms.channels.set_channel_any_mode("#api", True)
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

    with patch.object(comms.bus.log, "_opened_wire_snapshot", side_effect=toggle_after_boundary):
        first = comms.views.viewer_snapshot(str(tmp_path))
    old = next(view for view in first.channels if view.channel.name == "#api")
    assert not old.channel.any_mode
    assert old.last_activity == 0
    assert first.channel_unread["#api"] == 0

    second = comms.views.viewer_snapshot(str(tmp_path))
    new = next(view for view in second.channels if view.channel.name == "#api")
    assert new.channel.any_mode
    assert new.last_activity > 0
    assert second.channel_unread["#api"] == 1
    assert comms.bus.log.latest_sequence() == 1
    assert len(comms.views.channel_display_page("#any").messages) == 1


def test_opt_in_user_input_activity_uses_same_member_scope(tmp_path):
    comms = populated(tmp_path)
    comms.messaging.send_user_message("alice", "human input to member", worktree=str(tmp_path))
    off = next(
        view
        for view in comms.views.viewer_snapshot(str(tmp_path)).channels
        if view.channel.name == "#api"
    )
    assert off.last_user_input == 0
    comms.channels.set_channel_any_mode("#api", True)
    on = next(
        view
        for view in comms.views.viewer_snapshot(str(tmp_path)).channels
        if view.channel.name == "#api"
    )
    assert on.last_user_input > 0
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 0
    assert bodies(comms.views.channel_display_page("#api")) == ["human input to member"]
    assert bodies(comms.views.channel_history_page("#api")) == []


def test_alias_retag_and_tag_rename_reproject_without_new_wire_row(tmp_path):
    comms = populated(tmp_path)
    comms.messaging.send("alice", "bob", "old sender DM")
    comms.channels.set_channel_any_mode("api", True)
    high_water = comms.bus.log.latest_sequence()
    assert bodies(comms.views.channel_display_page("#api")) == ["old sender DM"]
    initial = {
        view.channel.name: view for view in comms.views.viewer_snapshot(str(tmp_path)).channels
    }
    assert initial["#api"].last_activity > 0

    comms.registry.rename("alice", "renamed")
    assert comms.bus.log.latest_sequence() == high_water
    assert bodies(comms.views.channel_display_page("#api")) == ["old sender DM"]
    renamed = comms.registry.require("renamed")
    comms.registry.register(
        replace(renamed, tags=frozenset({"ui"})), comms.registry.status("renamed")
    )
    assert comms.bus.log.latest_sequence() == high_water
    assert bodies(comms.views.channel_display_page("#api")) == []
    after = {
        view.channel.name: view for view in comms.views.viewer_snapshot(str(tmp_path)).channels
    }
    assert after["#api"].last_activity < initial["#api"].last_activity

    comms.registry.register(replace(comms.registry.require("renamed"), tags=frozenset({"api"})))
    comms.channels.rename_tag("api", "services")
    assert comms.bus.log.latest_sequence() == high_water
    assert comms.channels.catalog.read().resolve("#services").any_mode
    assert not comms.channels.catalog.read().resolve("#api").any_mode
    assert bodies(comms.views.channel_display_page("#services")) == ["old sender DM"]
    comms.channels.delete_tag("services")
    metadata = comms.channels.catalog.path
    assert not any(
        value.get("any_mode", False)
        for value in json.loads(metadata.read_text())["preferences"].values()
    )


def test_human_marker_unread_and_own_alias_exclusion(tmp_path):
    comms = populated(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send_user_message("#api", "owned before rename", worktree=str(tmp_path))
    comms.registry.rename(viewer, "human")
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 0
    comms.messaging.send("alice", "bob", "before threshold")
    comms.views.mark_channel_view_read("#api", worktree=str(tmp_path))
    comms.channels.set_channel_any_mode("#api", True)
    # The previously hidden DM was never painted, so enabling the broader
    # projection must expose it as unread despite the exact-channel marker.
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 1
    comms.messaging.send("bob", "alice", "after threshold")
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 2
    # The old user sender is still an alias of the current human viewer.
    comms.messaging.send_user_message("#api", "human owned", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 2
    comms.views.mark_channel_view_read("#api", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 0


def test_page_boundaries_and_byte_budget_match_target_pager(tmp_path):
    comms = populated(tmp_path)
    comms.messaging.send("alice", "bob", "first projected")
    comms.messaging.send("outsider", "#api", "second target")
    comms.messaging.send("bob", "alice", "third projected")
    comms.channels.set_channel_any_mode("#api", True)
    rows = comms.views.channel_display_page("#api").messages
    first_bytes = len((json.dumps(rows[0].to_wire()) + "\n").encode())
    assert bodies(
        comms.views.channel_display_page("#api", after=0, limit=20, max_bytes=first_bytes)
    ) == ["first projected"]
    head = comms.views.channel_display_page("#api", after=0, limit=1)
    assert head.has_newer and not head.has_older
    tail = comms.views.channel_display_page("#api", before=rows[-1].seq, limit=1)
    assert bodies(tail) == ["second target"] and tail.has_older and tail.has_newer
    with pytest.raises(ValueError, match="either before or after"):
        comms.views.channel_display_page("#api", before=2, after=1)


def test_fixed_opened_boundary_excludes_later_append(tmp_path):
    comms = populated(tmp_path)
    comms.messaging.send("alice", "bob", "within boundary")
    comms.channels.set_channel_any_mode("#api", True)
    original = comms.bus.log._opened_wire_snapshot
    started, finished = Event(), Event()

    def append():
        started.set()
        comms.messaging.send("alice", "bob", "outside boundary")
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

    with patch.object(comms.bus.log, "_opened_wire_snapshot", side_effect=with_append):
        assert bodies(comms.views.channel_display_page("#api")) == ["within boundary"]
    assert bodies(comms.views.channel_display_page("#api")) == [
        "within boundary",
        "outside boundary",
    ]


def test_failed_document_write_does_not_publish_mode(tmp_path):
    comms = populated(tmp_path)
    from agent_comms.catalog_store import ChannelCatalog

    with (
        patch.object(ChannelCatalog, "_write_unlocked", side_effect=OSError("catalog failure")),
        pytest.raises(OSError, match="catalog failure"),
    ):
        comms.channels.set_channel_any_mode("#api", True)
    assert not comms.channels.catalog.read().resolve("#api").any_mode
    assert not wire(tmp_path).channels.catalog.read().resolve("#api").any_mode
    comms.channels.set_channel_any_mode("#api", True)
    comms.channels.create_tag("later")
    assert wire(tmp_path).channels.catalog.read().resolve("#api").any_mode


def test_public_retag_and_send_cannot_leak_new_nonmember_into_page(tmp_path):
    comms = populated(tmp_path)
    comms.channels.set_channel_any_mode("#api", True)
    comms.messaging.send("alice", "bob", "before retag")
    original = comms.bus.log._opened_wire_snapshot
    started, finished = Event(), Event()

    def retag_and_send():
        started.set()
        comms.channels.update_tags("alice", remove=frozenset({"api"}), add=frozenset({"ui"}))
        comms.messaging.send("alice", "bob", "late nonmember")
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

    with patch.object(comms.bus.log, "_opened_wire_snapshot", side_effect=concurrent_public_change):
        assert bodies(comms.views.channel_display_page("#api")) == ["before retag"]
    later = comms.views.channel_display_page("#api")
    assert "late nonmember" not in bodies(later)
    assert all(message.target == "#api" for message in later.messages)
    assert any(message.membership is not None for message in later.messages)


def test_direct_retag_before_open_is_captured_with_its_source(tmp_path):
    comms = populated(tmp_path)
    comms.channels.set_channel_any_mode("#api", True)
    original = comms.bus.log._opened_wire_snapshot
    changed = False

    @contextmanager
    def mutate_before_open(*, need_sequence=True):
        nonlocal changed
        if not changed:
            changed = True
            alice = comms.registry.require("alice")
            comms.registry.register(replace(alice, tags=frozenset({"ui"})))
            comms.bus.publisher.publish_ordinary(
                Message("alice", "bob", "late direct", MessageType.INFO)
            )
        with original(need_sequence=need_sequence) as snapshot:
            yield snapshot

    with patch.object(comms.bus.log, "_opened_wire_snapshot", side_effect=mutate_before_open):
        assert bodies(comms.views.channel_display_page("#api")) == []
    assert changed
    assert bodies(comms.views.channel_display_page("#api")) == []
    assert bodies(comms.views.channel_display_page("#any")) == ["late direct"]


def test_direct_rename_and_own_send_do_not_inflate_unread(tmp_path):
    comms = populated(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("bob", "#api", "outside unread")
    original = comms.bus.log._opened_wire_snapshot
    changed = False

    @contextmanager
    def rename_and_send_before_open(*, need_sequence=True):
        nonlocal changed
        if not changed:
            changed = True
            comms.registry.rename(viewer, "human")
            human = comms.registry.require("human")
            comms.bus.publisher.publish_ordinary(
                Message("human", "#api", "owned unread", MessageType.INFO),
                _human_origin=HumanOrigin(human.name, human.created_at, human.worktree),
            )
        with original(need_sequence=need_sequence) as snapshot:
            yield snapshot

    with patch.object(comms.bus.log, "_opened_wire_snapshot", side_effect=rename_and_send_before_open):
        raced = comms.views.viewer_snapshot(str(tmp_path))
    assert changed
    assert raced.channel_unread["#api"] == 1
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 1


def test_public_mark_read_and_append_share_one_viewer_boundary(tmp_path):
    comms = populated(tmp_path)
    comms.messaging.send("bob", "#api", "row1")
    original = comms.bus.log._opened_wire_snapshot
    started, finished = Event(), Event()

    def mark_then_append():
        started.set()
        comms.views.mark_channel_view_read("#api", worktree=str(tmp_path))
        comms.messaging.send("bob", "#api", "row2")
        finished.set()

    @contextmanager
    def concurrent_public_mark(*, need_sequence=True):
        with original(need_sequence=need_sequence) as snapshot:
            if started.is_set():
                yield snapshot
                return
            writer = WorkerThread(target=mark_then_append, daemon=True)
            writer.start()
            assert started.wait(timeout=3) and not finished.is_set()
            try:
                yield snapshot
            finally:
                writer.join(timeout=3)
                assert finished.is_set()

    with patch.object(comms.bus.log, "_opened_wire_snapshot", side_effect=concurrent_public_mark):
        assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 1
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"] == 1


def test_busy_display_journey_uses_identity_not_presence_file_replacement(tmp_path):
    comms = populated(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("alice", viewer, "DM original")
    started, finished = Event(), Event()
    failures = []

    def burst():
        other = wire(tmp_path)
        try:
            started.set()
            for n in range(20):
                other.registry.heartbeat("alice")
                other.messaging.send("alice", "#api", f"burst {n}")
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    writer = WorkerThread(target=burst, daemon=True)
    writer.start()
    assert started.wait(timeout=3)
    try:
        for _ in range(20):
            page = comms.views.channel_display_page("#api", worktree=str(tmp_path))
            if page.newest_seq is not None:
                comms.views.mark_channel_view_read(
                    "#api", worktree=str(tmp_path), through=page.newest_seq,
                    expected_scope=page.display_scope,
                )
            dm = comms.views.dm_display_page("alice", worktree=str(tmp_path))
            assert bodies(dm) == ["DM original"]
            comms.views.mark_dm_view_read(
                "alice", worktree=str(tmp_path), through=dm.newest_seq,
                expected_display_basis=dm.display_basis,
            )
    finally:
        writer.join(timeout=10)
    assert finished.is_set() and not writer.is_alive() and not failures
    page = comms.views.channel_display_page("#api", worktree=str(tmp_path))
    assert bodies(page) == [f"burst {n}" for n in range(20)]
    comms.registry.rename("alice", "renamed")
    with pytest.raises(ValueError, match="incarnation changed"):
        comms.views.mark_dm_view_read(
            "alice", worktree=str(tmp_path), through=dm.newest_seq,
            expected_display_basis=dm.display_basis,
        )


def test_new_channel_during_boundary_is_not_falsely_unknown(tmp_path):
    comms = populated(tmp_path)
    original = comms.bus.log._opened_wire_snapshot
    added = False

    @contextmanager
    def register_before_open(*, need_sequence=True):
        nonlocal added
        if not added:
            added = True
            comms.registry.register(Thread("late", frozenset({"new"}), str(tmp_path)))
        with original(need_sequence=need_sequence) as snapshot:
            yield snapshot

    with patch.object(comms.bus.log, "_opened_wire_snapshot", side_effect=register_before_open):
        assert bodies(comms.views.channel_display_page("#new")) == []
    assert added
