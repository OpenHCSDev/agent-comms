import json
import os
from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_comms import (
    Activity,
    ActivityState,
    Channel,
    ChannelSort,
    ForkSpec,
    Message,
    MessageType,
    SavedView,
    Thread,
    ThreadSort,
    ThreadStatus,
    ThreadView,
    ViewKind,
    ViewMatch,
    ViewPredicate,
    invoke_tool,
    wire,
)


def setup_wire(path):
    comms = wire(path)
    for name, tags in (("a", {"api"}), ("b", {"ui"}), ("both", {"api", "ui"}), ("other", set())):
        comms.register(Thread(name=name, tags=frozenset(tags), worktree=str(path)))
    return comms


def test_named_union_membership_and_routing(tmp_path):
    comms = setup_wire(tmp_path)
    comms.set_channel("engineering", frozenset({"api", "ui"}))
    views = {view.channel.name: view for view in comms.channel_views()}
    assert set(views["#engineering"].members) == {"a", "b", "both"}
    assert set(views["#any"].members) == {"a", "b", "both", "other"}
    assert views["#none"].members == ("other",)
    assert set(views["#api"].members) == {"a", "both"}
    assert set(views["#ui"].members) == {"b", "both"}
    comms.send("other", "#engineering", "union delivery")
    comms.send("other", "#api", "api delivery")
    comms.send("other", "#ui", "ui delivery")
    assert len(comms.inbox("both")) == 3
    assert len(comms.incoming_page("both", after=0).messages) == 3
    assert len(comms.inbox("a")) == 2
    assert comms.pending_count("both", "#engineering") == 1
    assert len(comms.channel_history_page("#engineering").messages) == 1
    assert comms.acknowledge("both", "#engineering") == 1
    assert comms.pending_count("both") == 2
    comms.send("a", "other", "private")
    assert len(comms.channel_history("#any")) == 4
    assert len(comms.channel_history("#engineering")) == 1


def test_channel_history_is_owned_by_the_stored_target(tmp_path):
    comms = setup_wire(tmp_path)
    comms.set_channel("engineering", frozenset({"api", "ui"}))
    comms.send("other", "#engineering", "union target")
    comms.send("other", "#api", "exact api target")
    comms.send("other", "#ui", "exact ui target")
    assert [message.body for message in comms.channel_history("#engineering")] == ["union target"]
    assert [message.body for message in comms.channel_history("#api")] == ["exact api target"]


def test_channel_metadata_round_trips_without_changing_routing(tmp_path):
    comms = setup_wire(tmp_path)
    before = {view.channel.name: view.members for view in comms.channel_views()}
    channel = comms.set_channel_metadata("api", parent="#ui", archived=True)
    assert channel.parent == "#ui" and channel.archived

    observer = wire(tmp_path)
    projected = {view.channel.name: view for view in observer.channel_views()}
    assert projected["#api"].channel.parent == "#ui"
    assert projected["#api"].channel.archived
    assert {name: view.members for name, view in projected.items()} == before

    observer.send("other", "#api", "still routable")
    assert [message.body for message in observer.inbox("a")] == ["still routable"]
    with pytest.raises(ValueError, match="cycle"):
        observer.set_channel_metadata("ui", parent="#api", archived=False)
    assert observer.channel_catalog.resolve("#ui").parent is None


def test_saved_views_are_typed_persistent_and_non_routable(tmp_path):
    comms = setup_wire(tmp_path)
    view = SavedView(
        "api-and-ui",
        ViewKind.PARTICIPANTS,
        ViewPredicate(ViewMatch.ALL_OF, frozenset({"api", "ui"})),
        created_at=123,
    )
    assert comms.set_saved_view(view) == view
    assert wire(tmp_path).saved_views() == {"api-and-ui": view}
    assert [
        name for name, thread in comms.registry.all_threads().items() if view.matches(thread.tags)
    ] == ["both"]
    assert "#api-and-ui" not in comms.channels()
    with pytest.raises(ValueError, match="not a routable target"):
        comms.send("other", "#api-and-ui", "must fail")
    comms.delete_saved_view("api-and-ui")
    assert not wire(tmp_path).saved_views()


def test_any_is_non_routable_but_preserves_legacy_rows_as_global_history(tmp_path):
    comms = setup_wire(tmp_path)
    with pytest.raises(ValueError, match="not a routable target"):
        comms.send("other", "#any", "agent send rejected")
    with pytest.raises(ValueError, match="not a routable target"):
        comms.send_user_message("#any", "user send rejected", worktree=str(tmp_path))

    legacy = Message("other", "#any", "legacy any row", MessageType.INFO, timestamp=1, seq=1)
    comms.bus._path.write_text(json.dumps(legacy.to_wire()) + "\n")
    assert [message.body for message in comms.channel_history("#any")] == ["legacy any row"]


def test_tag_lifecycle_is_transactional_for_metadata_views_and_history(tmp_path):
    comms = setup_wire(tmp_path)
    comms.create_tag("child")
    comms.set_channel_sort("ui", ThreadSort.LAST_ACTIVITY)
    comms.set_channel_pinned("ui", True)
    ui_created = comms.channel_catalog.resolve("#ui").created_at
    comms.set_channel_metadata("api", parent="#ui", archived=True)
    comms.set_channel_metadata("child", parent="#api", archived=False)
    comms.set_saved_view(
        SavedView(
            "cross-team",
            ViewKind.PARTICIPANTS,
            ViewPredicate(ViewMatch.ALL_OF, frozenset({"api", "ui"})),
        )
    )
    comms.send("other", "#api", "target-owned historical row")

    before_conflict = comms.registry.snapshot()
    with pytest.raises(ValueError, match="already exists"):
        comms.rename_tag("api", "ui")
    assert comms.registry.snapshot() == before_conflict

    comms.rename_tag("ui", "docs")
    observer = wire(tmp_path)
    assert observer.channel_catalog.resolve("#api").parent == "#docs"
    assert observer.channel_catalog.resolve("#api").archived
    docs = observer.channel_catalog.resolve("#docs")
    assert docs.order is ThreadSort.LAST_ACTIVITY
    assert docs.created_at == ui_created
    assert docs.pinned
    assert observer.channel_catalog.resolve("#child").parent == "#api"
    assert observer.saved_views()["cross-team"].predicate.tags == {"api", "docs"}
    assert "#docs" in observer.channels() and "#ui" not in observer.channels()

    before = observer.registry.snapshot()
    with pytest.raises(ValueError, match="referenced by saved views"):
        observer.delete_tag("api")
    assert observer.registry.snapshot() == before
    assert observer.channel_catalog.resolve("#child").parent == "#api"

    observer.set_saved_view(
        SavedView(
            "cross-team",
            ViewKind.PARTICIPANTS,
            ViewPredicate(ViewMatch.ALL_OF, frozenset({"docs"})),
        )
    )
    observer.delete_tag("api")
    restarted = wire(tmp_path)
    assert restarted.channel_catalog.resolve("#child").parent is None
    assert not restarted.channel_catalog.resolve("#api").archived
    assert [message.body for message in restarted.channel_history("#api")] == [
        "target-owned historical row"
    ]
    assert restarted.saved_views()["cross-team"].predicate.tags == {"docs"}


def test_legacy_channel_deletion_clears_children_without_rewriting_history(tmp_path):
    comms = setup_wire(tmp_path)
    original_tags = {name: thread.tags for name, thread in comms.registry.all_threads().items()}
    comms.set_channel("team", frozenset({"ui"}))
    comms.set_channel_metadata("api", parent="#team", archived=False)
    comms.send("other", "#team", "legacy target row")
    comms.delete_channel("team")

    restarted = wire(tmp_path)
    assert restarted.channel_catalog.resolve("#api").parent is None
    assert {
        name: thread.tags for name, thread in restarted.registry.all_threads().items()
    } == original_tags
    assert [message.body for message in restarted.channel_history("#team")] == ["legacy target row"]


def test_deleted_legacy_channel_name_gets_fresh_order_and_creation_on_reuse(tmp_path):
    comms = setup_wire(tmp_path)
    with patch("agent_comms.channels.time.time", return_value=100):
        first = comms.set_channel("temporary", frozenset({"ui"}))
    comms.set_channel_sort("temporary", ThreadSort.LAST_ACTIVITY)
    comms.delete_channel("temporary")

    with patch("agent_comms.channels.time.time", return_value=200):
        recreated = comms.set_channel("temporary", frozenset({"ui"}))
    assert first.created_at == 100
    assert recreated.created_at == 200
    assert recreated.order is ThreadSort.CREATED


def test_deleted_exact_tag_gets_fresh_order_and_creation_on_reuse(tmp_path):
    comms = setup_wire(tmp_path)
    with patch("agent_comms.channels.time.time", return_value=100):
        comms.create_tag("temporary")
    first = comms.channel_catalog.resolve("#temporary")
    comms.set_channel_sort("temporary", ThreadSort.LAST_ACTIVITY)
    comms.delete_tag("temporary")

    with patch("agent_comms.channels.time.time", return_value=200):
        comms.create_tag("temporary")
    recreated = wire(tmp_path).channel_catalog.resolve("#temporary")
    assert first.created_at == 100
    assert recreated.created_at == 200
    assert recreated.order is ThreadSort.CREATED


def test_deleting_legacy_collision_preserves_revealed_exact_preferences(tmp_path):
    comms = setup_wire(tmp_path)
    comms.create_tag("team")
    comms.update_tags("other", add=frozenset({"team"}))
    comms.set_channel_sort("team", ThreadSort.LAST_ACTIVITY)
    comms.set_channel_pinned("team", True)
    comms.set_thread_pinned("team", "other", True)
    comms.set_channel_metadata("team", parent="#api", archived=True)
    tags, channels = comms.channel_catalog.read()
    channels["#team"] = Channel("team", frozenset({"api"}), ThreadSort.LAST_ACTIVITY)
    comms.channel_catalog.write(tags, channels)
    before = wire(tmp_path).channel_catalog.resolve("#team")
    assert before.exact and before.order is ThreadSort.LAST_ACTIVITY

    comms.delete_channel("team")
    observer = wire(tmp_path)
    revealed = observer.channel_catalog.resolve("#team")
    assert revealed.exact
    assert revealed.order is ThreadSort.LAST_ACTIVITY
    assert revealed.created_at == before.created_at
    assert revealed.pinned and revealed.parent == "#api" and revealed.archived
    assert observer.channel_catalog.pinned_threads("#team") == {"other"}


def test_saved_view_names_are_reserved_for_tags_and_legacy_channels(tmp_path):
    comms = setup_wire(tmp_path)
    predicate = ViewPredicate(ViewMatch.ANY_OF, frozenset({"api"}))
    with pytest.raises(ValueError, match="conflicts with channel"):
        comms.set_saved_view(SavedView("api", ViewKind.PARTICIPANTS, predicate))
    comms.set_channel("legacy", frozenset({"api"}))
    with pytest.raises(ValueError, match="conflicts with channel"):
        comms.set_saved_view(SavedView("legacy", ViewKind.PARTICIPANTS, predicate))

    comms.set_saved_view(
        SavedView(
            "reserved",
            ViewKind.PARTICIPANTS,
            predicate,
        )
    )
    before = comms.registry.snapshot()
    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.create_tag("reserved")
    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.update_tags("other", add=frozenset({"reserved"}))
    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.rename_tag("ui", "reserved")
    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.set_channel("reserved", frozenset({"api"}))
    assert comms.registry.snapshot() == before
    assert "reserved" not in comms.channel_catalog.tags()


def test_tag_names_cannot_collide_with_legacy_channel_targets(tmp_path):
    comms = setup_wire(tmp_path)
    comms.set_channel("team", frozenset({"api", "ui"}))
    before = comms.registry.snapshot()
    with pytest.raises(ValueError, match="conflicts with legacy channel"):
        comms.create_tag("team")
    with pytest.raises(ValueError, match="conflicts with legacy channel"):
        comms.update_tags("other", add=frozenset({"team"}))
    with pytest.raises(ValueError, match="conflicts with legacy channel"):
        comms.rename_tag("ui", "team")
    assert comms.registry.snapshot() == before
    assert comms.channel_catalog.resolve("#team").tags == {"api", "ui"}


def test_all_implicit_tag_introduction_paths_honor_name_reservations(tmp_path):
    from agent_comms.importing import ImportedMessage, ImportFormat, ImportRole, ImportSnapshot

    comms = setup_wire(tmp_path)
    predicate = ViewPredicate(ViewMatch.ANY_OF, frozenset({"api"}))
    comms.set_saved_view(SavedView("reserved", ViewKind.PARTICIPANTS, predicate))
    before = comms.registry.snapshot()

    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.register(Thread("registered", frozenset({"reserved"}), str(tmp_path)))
    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.claim_thread("claimed", tags=frozenset({"reserved"}), worktree=str(tmp_path))
    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.set_channel("team", frozenset({"api", "reserved"}))

    source = tmp_path / "foreign.json"
    source.write_text("{}")

    class SnapshotSource:
        @staticmethod
        def read(*_args):
            return ImportSnapshot(
                ImportFormat.OPENCODE,
                "foreign",
                str(tmp_path),
                "Foreign",
                (ImportedMessage(ImportRole.USER, "continue"),),
                "",
                1,
                0,
                (),
                "continue",
            )

    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.import_thread(
            source,
            SnapshotSource(),  # type: ignore[arg-type]
            name="imported",
            tags=frozenset({"reserved"}),
        )
    parent_session = tmp_path / "parent.jsonl"
    parent_session.write_text("{}\n")
    comms.register(Thread("parent", frozenset(), str(tmp_path), session_file=str(parent_session)))
    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.fork(
            ForkSpec("child", "parent", "task", frozenset({"reserved"})),
            pi_bin="/bin/true",
        )

    after = comms.registry.snapshot()
    assert set(after.threads) == {*before.threads, "parent"}
    assert not (tmp_path / "imported_sessions").exists()
    assert "#team" not in comms.channels()


def test_channel_metadata_and_saved_views_have_typed_tool_surfaces(tmp_path):
    comms = setup_wire(tmp_path)
    channel = invoke_tool(
        comms,
        "comms_set_channel_metadata",
        {"name": "api", "parent": "#ui", "archived": True},
    )
    assert channel["parent"] == "#ui" and channel["archived"] is True

    view = invoke_tool(
        comms,
        "comms_set_view",
        {
            "name": "cross-team",
            "kind": "participants",
            "match": "all_of",
            "tags": "api,ui",
        },
    )
    assert view["predicate"] == {"match": "all_of", "tags": ["api", "ui"]}
    listed = invoke_tool(comms, "comms_channels", {})
    assert [item["name"] for item in listed["views"]] == ["cross-team"]
    invoke_tool(comms, "comms_delete_view", {"name": "cross-team"})
    assert not invoke_tool(comms, "comms_channels", {})["views"]


def test_named_audience_cannot_replace_an_exact_tag_channel(tmp_path):
    comms = setup_wire(tmp_path)
    with pytest.raises(ValueError, match="cannot replace"):
        comms.set_channel("api", frozenset({"ui"}))
    assert comms.channel_catalog.resolve("#api").exact


def test_tag_operations_preserve_owner_and_update_all_views(tmp_path, monkeypatch):
    comms = setup_wire(tmp_path)
    original = comms.registry.require("other")
    monkeypatch.setenv("PI_AGENT_ID", "other")
    invoke_tool(comms, "comms_tags", {"action": "create", "name": "new"})
    assert "#new" in comms.channels()
    invoke_tool(comms, "comms_thread_tags", {"add": "new,api", "remove": "ui"})
    assert comms.registry.require("other") == replace(
        original, tags=frozenset({"new", "api"}), channel_scope_generation=1
    )
    invoke_tool(comms, "comms_set_channel", {"name": "team", "tags": "api,ui"})
    invoke_tool(comms, "comms_tags", {"action": "rename", "name": "api", "new_name": "backend"})
    assert "backend" in comms.registry.require("a").tags
    assert wire(tmp_path).channel_catalog.resolve("#team").tags == frozenset({"backend", "ui"})
    invoke_tool(comms, "comms_tags", {"action": "delete", "name": "backend"})
    assert not comms.registry.require("a").tags
    invoke_tool(comms, "comms_delete_channel", {"name": "team"})
    assert "#ui" in comms.channels()
    assert "#team" not in comms.channels()
    assert invoke_tool(comms, "comms_channels", {})["channels"]
    comms.registry.archive("a")
    assert "a" not in next(
        view.members for view in comms.channel_views() if view.channel.name == "#any"
    )
    assert comms.registry.status("a") is ThreadStatus.ARCHIVED


def test_pending_cache_observes_external_changes_without_rescanning(tmp_path):
    comms = setup_wire(tmp_path)
    other = wire(tmp_path)
    comms.set_channel("team", frozenset({"api"}))
    comms.send("other", "#team", "one")
    assert comms.pending_count("a") == 1
    with patch.object(
        comms.bus, "_iter_log_unlocked", side_effect=AssertionError("rescanned idle log")
    ):
        for _ in range(10):
            comms.heartbeat("a")
            assert comms.pending_count("a") == 1
    other.set_channel("team", frozenset({"ui"}))
    assert comms.pending_count("a") == 0
    assert comms.pending_count("b") == 1
    other.update_tags("a", add=frozenset({"ui"}))
    assert comms.pending_count("a") == 1
    other.acknowledge("a")
    assert comms.pending_count("a") == 0
    other.send("other", "#team", "two")
    assert comms.pending_count("a") == 1


def test_invalid_filters_and_reserved_channels(tmp_path):
    comms = setup_wire(tmp_path)
    for name in ("", "Bad Tag", "all", "any"):
        with pytest.raises(ValueError):
            comms.create_tag(name)
    with pytest.raises(ValueError):
        Channel("empty")
    with pytest.raises(ValueError):
        comms.set_channel("#any", frozenset({"api"}))
    with pytest.raises(ValueError):
        comms.delete_channel("#all")
    with pytest.raises(ValueError):
        comms.delete_channel("#api")


def test_thread_presentation_owns_lifecycle_precedence(tmp_path):
    thread = Thread("worker", frozenset(), str(tmp_path))
    working = Activity("worker", ActivityState.WORKING, "Running tests")
    view = ThreadView(thread, ThreadStatus.RUNNING, working, None, 0)
    assert view.presentation.busy
    assert view.presentation.summary == "Working · Running tests"
    stopped = replace(view, status=ThreadStatus.STOPPED)
    assert not stopped.presentation.busy
    assert stopped.presentation.summary == "Stopped"
    assert stopped.presentation.label == "○ worker"


def test_none_membership_and_independent_channel_order(tmp_path):
    from agent_comms import ThreadSort

    comms = setup_wire(tmp_path)
    comms.send("a", "#none", "for untagged agents")
    assert [message.body for message in comms.inbox("other")] == ["for untagged agents"]
    assert not comms.inbox("b")
    comms.set_channel_sort("#api", ThreadSort.LAST_MESSAGE)
    comms.set_channel_sort("#none", ThreadSort.LAST_ACTIVITY)
    views = {view.channel.name: view for view in wire(tmp_path).channel_views()}
    assert views["#api"].channel.order is ThreadSort.LAST_MESSAGE
    assert views["#none"].channel.order is ThreadSort.LAST_ACTIVITY
    assert views["#any"].channel.order is ThreadSort.CREATED
    assert views["#api"].members[0] == "a"
    comms.update_tags("other", add=frozenset({"ui"}))
    assert not next(view.members for view in comms.channel_views() if view.channel.name == "#none")


def test_channel_list_order_is_persistent_and_independent_of_viewer(tmp_path):
    comms = setup_wire(tmp_path)
    with patch("agent_comms.channels.time.time", return_value=200):
        first = comms.set_channel("zeta", frozenset({"ui"}))
    with patch("agent_comms.channels.time.time", return_value=300):
        second = comms.set_channel("alpha", frozenset({"api"}))
    assert first.created_at == 200 and second.created_at == 300

    def names(client):
        return [
            view.channel.name for view in client.channel_views() if view.channel.builtin is None
        ]

    comms.set_channel_order(ChannelSort.CREATED)
    created_names = names(wire(tmp_path))
    assert set(created_names) == {"#api", "#ui", "#alpha", "#zeta"}
    assert created_names.index("#alpha") < created_names.index("#zeta")
    comms.set_channel_sort("#zeta", ThreadSort.LAST_MESSAGE)
    assert comms.channel_catalog.resolve("#zeta").created_at == 200
    comms.set_activity("b", ActivityState.WORKING, "Active in zeta")
    comms.set_channel_order(ChannelSort.LAST_ACTIVITY)
    assert set(names(comms)[:2]) == {"#ui", "#zeta"}
    comms.send_user_message("#alpha", "Human input", worktree=str(tmp_path))
    comms.send("b", "#zeta", "Later agent output")
    invoke_tool(comms, "comms_sort_channels", {"order": "last_user_input"})
    assert names(wire(tmp_path))[0] == "#alpha"
    assert [view.channel.name for view in comms.coordination_snapshot("a").channels] == [
        view.channel.name for view in wire(tmp_path).coordination_snapshot("b").channels
    ]
    assert comms.coordination_snapshot().channel_order is ChannelSort.LAST_USER_INPUT
    # Reading or re-sorting must never redefine a channel's creation time.
    assert comms.channel_catalog.resolve("#alpha").created_at == 300
    with patch.object(comms.bus, "_iter_log_unlocked", side_effect=AssertionError("idle rescan")):
        comms.channel_views()


def test_membership_notices_follow_union_membership_and_do_not_wake(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("moving", frozenset(), str(tmp_path)))
    comms.register(Thread("peer", frozenset({"api"}), str(tmp_path), pid=os.getpid()))
    comms.set_channel("team", frozenset({"api", "ui"}))
    comms.update_tags("moving", add=frozenset({"api"}))
    joined = comms.channel_history("#team")
    assert len(joined) == 1 and joined[0].membership.value == "joined"
    assert joined[0].body == "moving joined #team" and not joined[0].starts_turn
    assert "moving" not in comms.last_sent_timestamps()
    comms.update_tags("moving", add=frozenset({"ui"}))
    comms.update_tags("moving", remove=frozenset({"api"}))
    assert len(comms.channel_history("#team")) == 1
    comms.update_tags("moving", remove=frozenset({"ui"}))
    history = wire(tmp_path).channel_history("#team")
    assert [message.membership.value for message in history] == ["joined", "left"]
    assert all(not message.starts_turn for message in history)
    assert not comms.coordination_snapshot().participants("#team")
    comms.begin_turn("peer", "active-turn")
    assert [
        person.thread.name for person in comms.coordination_snapshot().participants("#team")
    ] == ["peer"]
    comms.stop("peer")
    assert not comms.coordination_snapshot().participants("#team")


@pytest.mark.parametrize("order", tuple(ChannelSort))
def test_channel_pins_persist_and_partition_existing_order(tmp_path, order):
    comms = setup_wire(tmp_path)
    comms.set_channel_order(order)
    original = [view.channel.name for view in comms.channel_views()]
    observer = wire(tmp_path)
    observer.channel_views()  # Populate the observer cache before the external writes.
    for channel in ("#ui", "#none"):
        result = invoke_tool(comms, "comms_pin_channel", {"name": channel, "pinned": True})
        assert result["pinned"]
    views = observer.coordination_snapshot().channels
    assert [view.channel.name for view in views] == (
        [name for name in original if name in {"#ui", "#none"}]
        + [name for name in original if name not in {"#ui", "#none"}]
    )
    assert all(view.to_wire()["pinned"] for view in views[:2])
    assert observer.channel_catalog.list_order is order
    for channel in ("#ui", "#none"):
        observer.set_channel_pinned(channel, False)
    assert [view.channel.name for view in comms.channel_views()] == original


@pytest.mark.parametrize("order", tuple(ThreadSort))
def test_member_pins_are_persistent_channel_scoped_and_preserve_sort(tmp_path, order):
    comms = setup_wire(tmp_path)
    comms.set_channel("team", frozenset({"api", "ui"}))
    comms.set_channel_sort("#team", order)
    original = {view.channel.name: view.members for view in comms.channel_views()}
    result = invoke_tool(
        comms, "comms_pin_thread", {"channel": "team", "name": "a", "pinned": True}
    )
    assert result["pinned_members"] == ["a"]
    observer = wire(tmp_path)
    views = {view.channel.name: view for view in observer.channel_views()}
    assert views["#team"].members == ("a", *(name for name in original["#team"] if name != "a"))
    assert views["#any"].members == original["#any"]
    assert views["#any"].pinned_members == frozenset()
    assert views["#team"].channel.order is order
    observer.set_thread_pinned("team", "a", False)
    assert {view.channel.name: view.members for view in comms.channel_views()} == original


def test_pin_lifecycle_tracks_identity_and_membership(tmp_path):
    comms = setup_wire(tmp_path)
    comms.set_channel("team", frozenset({"api", "ui"}))
    comms.set_channel_pinned("team", True)
    comms.set_thread_pinned("team", "a", True)
    comms.set_thread_pinned("#any", "a", True)
    comms.registry.register(replace(comms.registry.require("a"), pid=os.getpid()))
    comms.rename_managed_thread("a", "renamed", owner_pid=os.getpid())
    observer = wire(tmp_path)
    assert observer.channel_catalog.pinned_threads("#team") == {"renamed"}
    assert observer.channel_catalog.pinned_threads("#any") == {"renamed"}
    comms.update_tags("renamed", remove=frozenset({"api"}))
    team = next(view for view in observer.channel_views() if view.channel.name == "#team")
    assert not team.pinned_members and "renamed" not in team.members
    comms.update_tags("renamed", add=frozenset({"api"}))
    team = next(view for view in observer.channel_views() if view.channel.name == "#team")
    assert team.pinned_members == {"renamed"} and team.members[0] == "renamed"
    # Removing a thread must not leave a pin for a future reuse of its name.
    comms.registry.unregister("renamed")
    comms.delete("renamed")
    assert not observer.channel_catalog.pinned_threads("#team")
    assert not observer.channel_catalog.pinned_threads("#any")
    comms.delete_channel("team")
    comms.set_channel("team", frozenset({"api"}))
    assert not observer.channel_catalog.resolve("#team").pinned


def test_tag_view_pins_follow_rename_and_clear_on_delete(tmp_path):
    comms = setup_wire(tmp_path)
    comms.set_channel_pinned("api", True)
    comms.set_thread_pinned("api", "a", True)
    comms.rename_tag("api", "backend")
    observer = wire(tmp_path)
    assert observer.channel_catalog.resolve("#backend").pinned
    assert observer.channel_catalog.pinned_threads("#backend") == {"a"}
    assert not observer.channel_catalog.pinned_threads("#api")
    comms.delete_tag("backend")
    comms.create_tag("backend")
    assert not observer.channel_catalog.resolve("#backend").pinned
    assert not observer.channel_catalog.pinned_threads("#backend")


def test_invalid_pins_do_not_mutate_catalog(tmp_path):
    comms = setup_wire(tmp_path)
    before = comms.channel_catalog.path.read_text()
    with pytest.raises(ValueError, match="Unknown channel"):
        comms.set_channel_pinned("missing", True)
    with pytest.raises(ValueError, match="Unknown channel"):
        comms.set_thread_pinned("missing", "a", True)
    with pytest.raises(ValueError, match="not a member"):
        comms.set_thread_pinned("api", "b", True)
    with pytest.raises(ValueError, match="must be boolean"):
        invoke_tool(comms, "comms_pin_channel", {"name": "api", "pinned": "false"})
    assert comms.channel_catalog.path.read_text() == before


def test_legacy_catalog_writers_cannot_erase_pin_preferences(tmp_path):
    comms = setup_wire(tmp_path)
    # Capture the catalog as an old detached executor would have loaded it.
    legacy = json.loads(comms.channel_catalog.path.read_text())
    observer = wire(tmp_path)
    observer.channel_views()
    comms.set_channel_pinned("api", True)
    comms.set_thread_pinned("api", "a", True)
    legacy["tags"].append("new-from-old-owner")
    comms.channel_catalog.path.write_text(json.dumps(legacy))
    views = {view.channel.name: view for view in observer.channel_views()}
    assert views["#api"].channel.pinned and views["#api"].pinned_members == {"a"}
    assert "#new-from-old-owner" in views
