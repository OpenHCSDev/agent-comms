"""The opt-in channel activity feed is a local display, never wire authority."""

import json
from dataclasses import replace
from threading import Event
from threading import Thread as WorkerThread

import pytest

from agent_comms.channel_targets import CommandTarget
from agent_comms.channels import AnyOfMatch, ViewPredicate, ActivityView
from agent_comms.comms import wire
from agent_comms.coordination_errors import DMPaintedBoundChanged, DMScopeChanged
from agent_comms.message_page import PageBounds
from agent_comms.page_traversal import LiveCursor, NewerTraversal
from agent_comms.read_basis import ChannelDisplayScope, DMDisplayScope, NamedViewer
from bus_record_fixture import prepare_native_comms


def test_missing_painted_scope_is_a_stale_bound():
    with pytest.raises(DMPaintedBoundChanged):
        ChannelDisplayScope("#api", frozenset({"#api"})).require_displayed()


def test_invalid_display_participants_are_typed_scope_changes(tmp_path):
    comms = populated(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    snapshot = comms.registry.snapshot()
    for first, peer in (("missing", "alice"), (viewer, "missing"), ("alice", "bob"), (viewer, viewer)):
        with pytest.raises(DMScopeChanged):
            DMDisplayScope.capture_human(first, peer, snapshot)


def populated(tmp_path):
    comms = prepare_native_comms(wire(tmp_path))
    for name, tags in (
        ("alice", {"api"}),
        ("carol", {"api"}),
        ("bob", {"ui"}),
        ("outsider", set()),
    ):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(tags), str(tmp_path)))
    return comms


def bodies(page):
    return [message.body for message in page.messages]


def test_default_off_and_opt_in_only_changes_display(tmp_path):
    comms = populated(tmp_path)
    comms.messaging.send("outsider", "#api", "exact target").message.message_id
    comms.messaging.send("alice", "bob", "member to outsider").message.message_id
    comms.messaging.send("bob", "carol", "outsider to member").message.message_id
    comms.messaging.send("outsider", "bob", "explicit @alice mention").message.message_id
    comms.messaging.send("outsider", "#ui", "unrelated channel").message.message_id
    comms.messaging.send("outsider", "bob", "ordinary outsider DM").message.message_id
    with comms.bus.snapshot(viewer=NamedViewer("alice")) as (basis, source):
        before_delivery = comms.bus.pending_counts_opened(source, basis)
    before_bus = comms.bus.bus.page(comms.bus.bus.revision() + 1, 100)
    assert bodies(comms.views.history_page(CommandTarget.named("#api"))) == ["exact target"]
    assert bodies(comms.views.channel_display_page("#api")) == ["exact target"]

    selected = comms.channels.set_channel_any_mode("#api", True)
    assert selected.exact and selected.any_mode
    assert wire(tmp_path).bus.require_channel("#api").any_mode
    page = comms.views.channel_display_page("#api")
    assert bodies(page) == [
        "exact target",
        "member to outsider",
        "outsider to member",
        "explicit @alice mention",
    ]
    assert [message.target for message in page.messages] == ["#api", "bob", "carol", "bob"]
    assert len({message.seq for message in page.messages}) == len(page.messages)
    assert bodies(comms.views.history_page(CommandTarget.named("#api"))) == ["exact target"]
    assert [message.body for message in list(comms.views.history_page(CommandTarget.named("#api")).messages)] == ["exact target"]
    assert bodies(comms.views.channel_display_page("#ui")) == ["unrelated channel"]
    assert comms.bus.bus.page(comms.bus.bus.revision() + 1, 100) == before_bus
    with comms.bus.snapshot(viewer=NamedViewer("alice")) as (basis, source):
        assert comms.bus.pending_counts_opened(source, basis) == before_delivery
    assert not comms.channels.set_channel_any_mode("api", False).any_mode
    assert bodies(comms.views.channel_display_page("#api")) == ["exact target"]


def test_mode_rejects_builtins_union_view_and_unknown_without_sidecar_mutation(tmp_path):
    comms = populated(tmp_path)
    comms.channels.set_saved_view(
        ActivityView('engineering', ViewPredicate(AnyOfMatch, frozenset({'api', 'ui'})))
    )
    comms.channels.set_saved_view(
        ActivityView('saved', ViewPredicate(AnyOfMatch, frozenset({'api'})))
    )
    metadata = comms.channels.catalog.path
    before = metadata.read_bytes() if metadata.exists() else None
    for channel in ("#any", "#all", "#none", "#engineering", "#saved", "#missing"):
        with pytest.raises(ValueError):
            comms.channels.set_channel_any_mode(channel, True)
    with pytest.raises(ValueError):
        comms.channels.set_channel_any_mode("#api", 1)
    assert (metadata.read_bytes() if metadata.exists() else None) == before
    with pytest.raises(ValueError):
        comms.views.channel_display_page("#missing")


def test_opt_in_user_input_activity_uses_same_member_scope(tmp_path):
    comms = populated(tmp_path)
    comms.messaging.send_user_message(CommandTarget.named('alice'), "human input to member", worktree=str(tmp_path))
    off = comms.views.viewer_snapshot(str(tmp_path)).channels["#api"]
    assert off.last_user_input == 0
    comms.channels.set_channel_any_mode("#api", True)
    on = comms.views.viewer_snapshot(str(tmp_path)).channels["#api"]
    assert on.last_user_input > 0
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"].count == 0
    assert bodies(comms.views.channel_display_page("#api")) == ["human input to member"]
    assert bodies(comms.views.history_page(CommandTarget.named("#api"))) == []


def test_alias_retag_and_tag_rename_reproject_without_new_wire_row(tmp_path):
    comms = populated(tmp_path)
    comms.messaging.send("alice", "bob", "old sender DM").message.message_id
    comms.channels.set_channel_any_mode("api", True)
    high_water = comms.bus.bus.revision()
    assert bodies(comms.views.channel_display_page("#api")) == ["old sender DM"]
    initial = comms.views.viewer_snapshot(str(tmp_path)).channels
    assert initial["#api"].last_activity > 0

    comms.registry.rename("alice", "renamed")
    assert comms.bus.bus.revision() == high_water
    assert bodies(comms.views.channel_display_page("#api")) == ["old sender DM"]
    renamed = comms.registry.require("renamed")
    comms.registry.register(
        replace(renamed, tags=frozenset({"ui"})), comms.registry.status("renamed")
    )
    assert comms.bus.bus.revision() == high_water
    assert bodies(comms.views.channel_display_page("#api")) == []
    after = comms.views.viewer_snapshot(str(tmp_path)).channels
    assert after["#api"].last_activity < initial["#api"].last_activity

    comms.registry.register(replace(comms.registry.require("renamed"), tags=frozenset({"api"})))
    comms.channels.rename_tag("api", "services")
    assert comms.bus.bus.revision() == high_water
    assert comms.bus.require_channel("#services").any_mode
    with pytest.raises(ValueError):
        comms.bus.require_channel("#api")
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
    comms.messaging.send_user_message(CommandTarget.named('#api'), "owned before rename", worktree=str(tmp_path))
    comms.registry.rename(viewer, "human")
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"].count == 0
    comms.messaging.send("alice", "bob", "before threshold").message.message_id
    comms.views.mark_user_view_read("#api", worktree=str(tmp_path))
    comms.channels.set_channel_any_mode("#api", True)
    # The previously hidden DM was never painted, so enabling the broader
    # projection must expose it as unread despite the exact-channel marker.
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"].count == 1
    comms.messaging.send("bob", "alice", "after threshold").message.message_id
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"].count == 2
    # The old user sender is still an alias of the current human viewer.
    comms.messaging.send_user_message(CommandTarget.named('#api'), "human owned", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"].count == 2
    comms.views.mark_user_view_read("#api", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#api"].count == 0


def test_page_boundaries_match_target_pager(tmp_path):
    comms = populated(tmp_path)
    comms.messaging.send("alice", "bob", "first projected").message.message_id
    comms.messaging.send("outsider", "#api", "second target").message.message_id
    comms.messaging.send("bob", "alice", "third projected").message.message_id
    comms.channels.set_channel_any_mode("#api", True)
    rows = comms.views.channel_display_page("#api").messages
    head = comms.views.channel_display_page("#api", bounds=PageBounds(NewerTraversal(), 1))
    assert head.has_newer and not head.has_older
    tail = comms.views.channel_display_page("#api", bounds=PageBounds(LiveCursor(rows[-1].seq).older(), 1))
    assert bodies(tail) == ["second target"] and tail.has_older and tail.has_newer


def test_busy_display_journey_uses_identity_not_presence_file_replacement(tmp_path):
    comms = populated(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.messaging.send("alice", viewer, "DM original").message.message_id
    started, finished = Event(), Event()
    failures = []

    def burst():
        other = wire(tmp_path)
        try:
            started.set()
            for n in range(20):
                other.registry.heartbeat("alice")
                other.messaging.send("alice", "#api", f"burst {n}").message.message_id
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    writer = WorkerThread(target=burst, daemon=True)
    writer.start()
    assert started.wait(timeout=3)
    try:
        for _ in range(20):
            page = comms.views.channel_display_page("#api", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
            if page.newest_seq is not None:
                comms.views.mark_page_read(page, worktree=str(tmp_path))
            dm = comms.views.dm_display_page("alice", worktree=str(tmp_path))
            assert bodies(dm) == ["DM original"]
            comms.views.mark_page_read(dm, worktree=str(tmp_path))
    finally:
        writer.join(timeout=10)
    assert finished.is_set() and not writer.is_alive() and not failures
    page = comms.views.channel_display_page("#api", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
    assert bodies(page) == [f"burst {n}" for n in range(20)]
    comms.registry.rename("alice", "renamed")
    with pytest.raises(DMScopeChanged):
        comms.views.mark_page_read(dm, worktree=str(tmp_path))


def test_views_answer_how_far_the_bus_has_grown(tmp_path):
    comms = populated(tmp_path)
    assert comms.views.latest_sequence() == 0
    comms.messaging.send("outsider", "#api", "to a channel").message.message_id
    comms.messaging.send("alice", "bob", "to someone else").message.message_id
    # Every message counts, not only those one conversation shows.
    newest_in_channel = comms.views.channel_display_page("#api").messages[-1].seq
    assert comms.views.latest_sequence() == comms.bus.bus.revision() > newest_in_channel
