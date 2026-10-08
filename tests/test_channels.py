import os
from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_comms import invoke_tool
from agent_comms.activity import Activity, ActivityState
from agent_comms.channels import AllOfMatch, AnyOfMatch, Channel, SavedView, ViewKind, ViewPredicate
from agent_comms.child_process import ProcessIdentity
from agent_comms.channel_management import (
    ArchiveThreadsTagDisposition, DeleteThreadsTagDisposition,
    DeleteExclusiveInactiveThreadsTagDisposition,
)
from agent_comms.comms import wire
from agent_comms.display_order import ChannelSort, ThreadSort
from agent_comms.errors import RelationViolationError
from agent_comms.presentation import ThreadView
from agent_comms.thread_management import ForkSpec
from agent_comms.thread_status import ArchivedThreadStatus, RunningThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread


def setup_wire(path):
    comms = wire(path)
    for name, tags in (("a", {"api"}), ("b", {"ui"}), ("both", {"api", "ui"}), ("other", set())):
        comms.registry.declare(Thread(name=name, tags=frozenset(tags), worktree=str(path)))
    return comms


def test_channel_metadata_round_trips_without_changing_routing(tmp_path):
    comms = setup_wire(tmp_path)
    before = {view.channel.name: view.members for view in comms.views.channel_views()}
    channel = comms.channels.set_channel_metadata("api", parent="#ui", archived=True)
    assert channel.parent == "#ui" and channel.archived

    observer = wire(tmp_path)
    projected = {view.channel.name: view for view in observer.views.channel_views(show_archived=True)}
    assert projected["#api"].channel.parent == "#ui"
    assert projected["#api"].channel.archived
    assert {name: view.members for name, view in projected.items()} == before
    assert "#api" not in {view.channel.name for view in observer.views.channel_views()}
    assert "#api" in observer.channels.catalog.read().views(observer.registry.all_threads())

    observer.messaging.send("other", "#api", "still routable")
    assert [message.body for message in observer.bus.inbox("a")] == ["still routable"]
    with pytest.raises(ValueError, match="cycle"):
        observer.channels.set_channel_metadata("ui", parent="#api", archived=False)
    assert observer.channels.catalog.read().resolve("#ui").parent is None

def test_tag_cohort_refuses_active_owners_before_any_registry_publication(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread('inactive', frozenset({'cohort'}), str(tmp_path)), StoppedThreadStatus())
    comms.registry.declare(Thread('active', frozenset({'cohort'}), str(tmp_path),
                                 process_identity=ProcessIdentity.capture(os.getpid())), RunningThreadStatus())
    original = comms.registry.snapshot()
    catalog = comms.channels.catalog.read()
    for disposition in (ArchiveThreadsTagDisposition(), DeleteThreadsTagDisposition()):
        with pytest.raises(ValueError) as refusal:
            comms.channels.delete_tag('cohort', disposition=disposition)
        assert str(refusal.value) == disposition.confirmation('cohort')
        with pytest.raises(RelationViolationError, match='stopped|Stop a running'):
            comms.channels.delete_tag('cohort', disposition=disposition, confirmed=True)
        assert comms.registry.snapshot() == original
        assert comms.channels.catalog.read() == catalog
        assert ProcessIdentity.capture(os.getpid()) == original.require('active').process_identity


def test_exclusive_tag_deletion_uses_original_storage_and_preserves_survivors(tmp_path):
    comms = wire(tmp_path / 'wire')
    saved = {}
    for name, tags, status in (
        ('single', {'remove'}, StoppedThreadStatus()),
        ('archived', {'remove'}, ArchivedThreadStatus()),
        ('multi', {'remove', 'keep'}, StoppedThreadStatus()),
        ('active', {'remove'}, RunningThreadStatus()),
        ('untouched', {'keep'}, StoppedThreadStatus()),
    ):
        path = tmp_path / (name + '.jsonl')
        path.write_text('{"original_saved_history":"' + name + '"}\n')
        saved[name] = path.read_bytes()
        process = ProcessIdentity.capture(os.getpid()) if name == 'active' else None
        comms.registry.declare(Thread(name, frozenset(tags), str(tmp_path),
                                     session_file=str(path), process_identity=process), status)
    comms.channels.set_channel_pinned('remove', True)
    comms.channels.set_thread_pinned('keep', 'multi', True)
    original_active = comms.registry.require('active')
    original_multi = comms.registry.require('multi')
    disposition = DeleteExclusiveInactiveThreadsTagDisposition()
    with pytest.raises(ValueError, match='only inactive'):
        comms.channels.delete_tag('remove', disposition=disposition)
    assert comms.registry.require('single').tags == {'remove'}
    result = comms.channels.delete_tag('remove', disposition=disposition, confirmed=True)
    assert result.removed_tag
    assert {row.name for row in result.removed_threads} == {'single', 'archived'}
    assert set(comms.registry.all_threads()) == {'multi', 'active', 'untouched'}
    assert comms.registry.require('multi').tags == {'keep'}
    assert comms.registry.require('multi').incarnation == original_multi.incarnation
    active = comms.registry.require('active')
    assert not active.tags and active.process_identity == original_active.process_identity
    assert comms.registry.status('active').active and active.process_alive
    catalog = comms.channels.catalog.read()
    assert 'remove' not in catalog.all_tags(comms.registry.all_threads())
    assert '#remove' not in catalog.preferences
    assert catalog.pinned_threads('#keep') == {'multi'}
    assert {name: (tmp_path / (name + '.jsonl')).read_bytes() for name in saved} == saved


def test_exclusive_tag_deletion_never_deletes_a_stopped_owner_that_is_still_alive(tmp_path):
    comms = wire(tmp_path / 'wire')
    original = Thread('still-alive', frozenset({'remove'}), str(tmp_path),
                      process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(original, StoppedThreadStatus())
    result = comms.channels.delete_tag('remove',
        disposition=DeleteExclusiveInactiveThreadsTagDisposition(), confirmed=True)
    assert not result.removed_threads
    surviving = comms.registry.require('still-alive')
    assert surviving.process_identity == original.process_identity and surviving.process_alive
    assert not surviving.tags


def test_tag_removal_commits_cohort_once_and_preserves_current_records(tmp_path):
    import cProfile
    import pstats

    comms = wire(tmp_path / 'wire')
    for index in range(40):
        comms.registry.declare(Thread(f'member-{index}', frozenset({'remove', 'keep'}),
                                     str(tmp_path)), StoppedThreadStatus())
    originals = tuple(comms.registry.all_threads().values())
    current = replace(originals[0], title='new title', task='new task', model='selected/model')
    comms.registry.register(current, ArchivedThreadStatus())
    before = comms.registry.snapshot()
    profile = cProfile.Profile()
    with profile:
        comms.channels._change_tag('remove', None)
    writes = sum(values[0] for (_, _, name), values in pstats.Stats(profile).stats.items()
                 if name == 'save_unlocked')
    assert writes == 1
    after = comms.registry.snapshot()
    for name, thread in before.threads.items():
        assert after.require(name) == replace(thread, tags=frozenset({'keep'}),
                                              channel_scope_generation=thread.channel_scope_generation + 1)
        assert after.status(name) == before.status(name)
    assert 'remove' not in comms.channels.catalog.read().all_tags(after.threads)
    # An acquired cohort may predate unrelated metadata changes. Tag mutation
    # must use the locked current records, not write those older Thread values.
    comms.registry.change_tag(originals, 'keep', 'renamed')
    latest = comms.registry.require(current.name)
    assert latest.title == current.title and latest.task == current.task and latest.model == current.model
    assert latest.tags == frozenset({'renamed'})
    assert comms.registry.status(current.name) == ArchivedThreadStatus()


def test_new_tag_disposition_is_declared_in_original_editor_and_tool_catalog(tmp_path):
    from agent_comms.cli_commands import CliCommand
    from agent_comms.field_codec import FieldCodec
    from agent_comms.tools import CommsTagsTool, tool_catalog

    comms = setup_wire(tmp_path)
    action = next(action for action in CliCommand.target_catalog(comms, '#api', project=str(tmp_path))
                  if action.declaration.declared_name == 'delete-tag')
    disposition = DeleteExclusiveInactiveThreadsTagDisposition
    choices = next(field.choices for field in action.editable_fields if field.name == 'disposition')
    assert (disposition.label, FieldCodec.encode(disposition)) in choices
    edited = action.edited({'disposition': FieldCodec.encode(disposition)})
    assert 'only inactive' in edited.confirmation
    tag_tool = next(tool for tool in tool_catalog() if tool['name'] == CommsTagsTool.declared_name)
    assert FieldCodec.encode(disposition) in str(tag_tool['parameters']['properties']['disposition'])


def test_saved_views_are_typed_persistent_and_non_routable(tmp_path):
    comms = setup_wire(tmp_path)
    view = SavedView(
        "api-and-ui",
        ViewKind.PARTICIPANTS,
        ViewPredicate(AllOfMatch, frozenset({"api", "ui"})),
        created_at=123,
    )
    assert comms.channels.set_saved_view(view) == view
    assert wire(tmp_path).channels.catalog.read().saved_views == {"api-and-ui": view}
    assert [
        name
        for name, thread in comms.registry.all_threads().items()
        if view.predicate.matches(thread.tags)
    ] == ["both"]
    assert "#api-and-ui" in comms.channels.channels()
    with pytest.raises(ValueError, match="not a routable target"):
        comms.messaging.send("other", "#api-and-ui", "must fail")
    comms.channels.delete_saved_view("api-and-ui")
    assert not wire(tmp_path).channels.catalog.read().saved_views


def test_any_is_non_routable(tmp_path):
    comms = setup_wire(tmp_path)
    with pytest.raises(ValueError, match="not a routable target"):
        comms.messaging.send("other", "#any", "agent send rejected")
    with pytest.raises(ValueError, match="not a routable target"):
        comms.messaging.send_user_message("#any", "user send rejected", worktree=str(tmp_path))


def test_tag_lifecycle_is_transactional_for_metadata_views_and_history(tmp_path):
    comms = setup_wire(tmp_path)
    comms.channels.create_tag("child")
    comms.channels.set_channel_sort("ui", ThreadSort.LAST_ACTIVITY)
    comms.channels.set_channel_pinned("ui", True)
    ui_created = comms.channels.catalog.read().resolve("#ui").created_at
    comms.channels.set_channel_metadata("api", parent="#ui", archived=True)
    comms.channels.set_channel_metadata("child", parent="#api", archived=False)
    comms.channels.set_saved_view(
        SavedView(
            "cross-team",
            ViewKind.PARTICIPANTS,
            ViewPredicate(AllOfMatch, frozenset({"api", "ui"})),
        )
    )
    comms.messaging.send("other", "#api", "target-owned historical row")

    before_conflict = comms.registry.snapshot()
    with pytest.raises(ValueError, match="already exists"):
        comms.channels.rename_tag("api", "ui")
    assert comms.registry.snapshot() == before_conflict

    comms.channels.rename_tag("ui", "docs")
    observer = wire(tmp_path)
    assert observer.channels.catalog.read().resolve("#api").parent == "#docs"
    assert observer.channels.catalog.read().resolve("#api").archived
    docs = observer.channels.catalog.read().resolve("#docs")
    assert docs.order is ThreadSort.LAST_ACTIVITY
    assert docs.created_at == ui_created
    assert docs.pinned
    assert observer.channels.catalog.read().resolve("#child").parent == "#api"
    assert observer.channels.catalog.read().saved_views["cross-team"].predicate.tags == {
        "api",
        "docs",
    }
    assert "#docs" in observer.channels.channels() and "#ui" not in observer.channels.channels()

    before = observer.registry.snapshot()
    observer.channels.delete_tag("api")
    after = observer.registry.snapshot()
    assert {name: thread.incarnation for name, thread in after.threads.items()} == {
        name: thread.incarnation for name, thread in before.threads.items()
    }
    assert all("api" not in thread.tags for thread in after.threads.values())
    assert observer.channels.catalog.read().saved_views["cross-team"].predicate.tags == {"api", "docs"}
    assert observer.channels.catalog.read().resolve("#child").parent is None

    observer.channels.set_saved_view(
        SavedView(
            "cross-team",
            ViewKind.PARTICIPANTS,
            ViewPredicate(AllOfMatch, frozenset({"docs"})),
        )
    )
    restarted = wire(tmp_path)
    assert restarted.channels.catalog.read().resolve("#child").parent is None
    assert not restarted.channels.catalog.read().resolve("#api").archived
    assert [message.body for message in restarted.views.channel_history("#api")] == [
        "target-owned historical row"
    ]
    assert restarted.channels.catalog.read().saved_views["cross-team"].predicate.tags == {"docs"}


def test_deleted_exact_tag_gets_fresh_order_and_creation_on_reuse(tmp_path):
    comms = setup_wire(tmp_path)
    with patch("agent_comms.channels.time.time", return_value=100):
        comms.channels.create_tag("temporary")
    first = comms.channels.catalog.read().resolve("#temporary")
    comms.channels.set_channel_sort("temporary", ThreadSort.LAST_ACTIVITY)
    comms.channels.delete_tag("temporary")

    with patch("agent_comms.channels.time.time", return_value=200):
        comms.channels.create_tag("temporary")
    recreated = wire(tmp_path).channels.catalog.read().resolve("#temporary")
    assert first.created_at == 100
    assert recreated.created_at == 200
    assert recreated.order is ThreadSort.CREATED


def test_saved_view_names_are_reserved_for_tags_and_legacy_channels(tmp_path):
    comms = setup_wire(tmp_path)
    predicate = ViewPredicate(AnyOfMatch, frozenset({"api"}))
    with pytest.raises(ValueError, match="conflicts with channel"):
        comms.channels.set_saved_view(SavedView("api", ViewKind.PARTICIPANTS, predicate))

    comms.channels.set_saved_view(
        SavedView(
            "reserved",
            ViewKind.PARTICIPANTS,
            predicate,
        )
    )
    before = comms.registry.snapshot()
    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.channels.create_tag("reserved")
    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.channels.update_tags("other", add=frozenset({"reserved"}))
    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.channels.rename_tag("ui", "reserved")
    assert comms.registry.snapshot() == before
    assert "reserved" not in comms.channels.catalog.read().all_tags(comms.registry.all_threads())


def test_all_implicit_tag_introduction_paths_honor_name_reservations(tmp_path):
    from agent_comms.importing import ImportedMessage, ImportFormat, ImportRole, ImportSnapshot

    comms = setup_wire(tmp_path)
    predicate = ViewPredicate(AnyOfMatch, frozenset({"api"}))
    comms.channels.set_saved_view(SavedView("reserved", ViewKind.PARTICIPANTS, predicate))
    before = comms.registry.snapshot()

    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.registry.declare(Thread("registered", frozenset({"reserved"}), str(tmp_path)))
    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.threads.claim_thread("claimed", tags=frozenset({"reserved"}), worktree=str(tmp_path))

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
        comms.threads.import_thread(
            source,
            SnapshotSource(),  # type: ignore[arg-type]
            name="imported",
            tags=frozenset({"reserved"}),
        )
    parent_session = tmp_path / "parent.jsonl"
    parent_session.write_text("{}\n")
    comms.registry.declare(
        Thread("parent", frozenset(), str(tmp_path), session_file=str(parent_session))
    )
    with pytest.raises(ValueError, match="reserved by a saved view"):
        comms.threads.fork(
            ForkSpec("child", "parent", "task", frozenset({"reserved"})),
            pi_bin="/bin/true",
        )

    after = comms.registry.snapshot()
    assert set(after.threads) == {*before.threads, "parent"}
    assert not (tmp_path / "imported_sessions").exists()
    assert "#team" not in comms.channels.channels()


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


def test_tag_operations_preserve_owner_and_update_all_views(tmp_path, monkeypatch):
    comms = setup_wire(tmp_path)
    original = comms.registry.require("other")
    monkeypatch.setenv("PI_AGENT_ID", "other")
    invoke_tool(comms, "comms_tags", {"action": "create", "name": "new"})
    assert "#new" in comms.channels.channels()
    invoke_tool(comms, "comms_thread_tags", {"add": "new,api", "remove": "ui"})
    assert comms.registry.require("other") == replace(
        original, tags=frozenset({"new", "api"}), channel_scope_generation=1
    )
    invoke_tool(
        comms,
        "comms_set_view",
        {"name": "team", "kind": "participants", "match": "any_of", "tags": "api,ui"},
    )
    invoke_tool(comms, "comms_tags", {"action": "rename", "name": "api", "new_name": "backend"})
    assert "backend" in comms.registry.require("a").tags
    assert wire(tmp_path).channels.catalog.read().resolve("#team").tags == frozenset(
        {"backend", "ui"}
    )
    invoke_tool(comms, "comms_delete_view", {"name": "team"})
    invoke_tool(comms, "comms_tags", {"action": "delete", "name": "backend"})
    assert not comms.registry.require("a").tags
    assert "#ui" in comms.channels.channels()
    assert "#team" not in comms.channels.channels()
    assert invoke_tool(comms, "comms_channels", {})["channels"]
    comms.registry.archive("a")
    assert "a" not in next(
        view.members for view in comms.views.channel_views() if view.channel.name == "#any"
    )
    assert comms.registry.status("a") == ArchivedThreadStatus()


def test_pending_cache_observes_external_changes_without_rescanning(tmp_path):
    comms = setup_wire(tmp_path)
    other = wire(tmp_path)
    comms.messaging.send("other", "#api", "one")
    assert comms.bus.pending_count("a") == 1
    with patch.object(
        comms.bus.log, "_iter_log_unlocked", side_effect=AssertionError("rescanned idle log")
    ):
        for _ in range(10):
            comms.threads.heartbeat("a")
            assert comms.bus.pending_count("a") == 1
    other.channels.update_tags("a", remove=frozenset({"api"}))
    assert comms.bus.pending_count("a") == 0
    other.channels.update_tags("b", add=frozenset({"api"}))
    assert [m.body for m in comms.bus.inbox("b") if m.membership is None] == ["one"]
    other.channels.update_tags("a", add=frozenset({"api"}))
    assert [m.body for m in comms.bus.inbox("a") if m.membership is None] == ["one"]
    other.messaging.acknowledge("a")
    assert comms.bus.pending_count("a") == 0
    other.messaging.send("other", "#api", "two")
    assert comms.bus.pending_count("a") == 1


def test_invalid_filters_and_reserved_channels(tmp_path):
    comms = setup_wire(tmp_path)
    for name in ("", "Bad Tag", "all", "any"):
        with pytest.raises(ValueError):
            comms.channels.create_tag(name)
    with pytest.raises(ValueError):
        Channel("empty")
    with pytest.raises(ValueError):
        Channel("#team", frozenset({"api", "ui"}))


def test_thread_presentation_owns_lifecycle_precedence(tmp_path):
    thread = Thread("worker", frozenset(), str(tmp_path))
    working = Activity("worker", ActivityState.WORKING, "Running tests")
    view = ThreadView(thread, RunningThreadStatus(), working, None, 0)
    assert view.presentation.busy
    assert view.presentation.summary == "Working · Running tests"
    stopped = replace(view, status=StoppedThreadStatus())
    assert not stopped.presentation.busy
    assert stopped.presentation.summary == "Stopped"
    assert stopped.presentation.label == "○ worker"


def test_none_membership_and_independent_channel_order(tmp_path):
    from agent_comms.display_order import ThreadSort

    comms = setup_wire(tmp_path)
    comms.messaging.send("a", "#none", "for untagged agents")
    assert [message.body for message in comms.bus.inbox("other")] == ["for untagged agents"]
    assert not comms.bus.inbox("b")
    comms.channels.set_channel_sort("#api", ThreadSort.LAST_MESSAGE)
    comms.channels.set_channel_sort("#none", ThreadSort.LAST_ACTIVITY)
    views = {view.channel.name: view for view in wire(tmp_path).views.channel_views()}
    assert views["#api"].channel.order is ThreadSort.LAST_MESSAGE
    assert views["#none"].channel.order is ThreadSort.LAST_ACTIVITY
    assert views["#any"].channel.order is ThreadSort.CREATED
    assert views["#api"].members[0] == "a"
    comms.channels.update_tags("other", add=frozenset({"ui"}))
    assert not next(
        view.members for view in comms.views.channel_views() if view.channel.name == "#none"
    )


def test_channel_list_order_is_persistent_and_independent_of_viewer(tmp_path):
    comms = setup_wire(tmp_path)
    with patch("agent_comms.channels.time.time", return_value=200):
        comms.channels.create_tag("zeta")
        first = comms.channels.catalog.read().resolve("#zeta")
    with patch("agent_comms.channels.time.time", return_value=300):
        comms.channels.create_tag("alpha")
        second = comms.channels.catalog.read().resolve("#alpha")
    assert first.created_at == 200 and second.created_at == 300
    comms.channels.update_tags("b", add=frozenset({"zeta"}))

    def names(client):
        return [
            view.channel.name
            for view in client.views.channel_views()
            if view.channel.builtin is None
        ]

    comms.channels.set_channel_order(ChannelSort.CREATED)
    created_names = names(wire(tmp_path))
    assert set(created_names) == {"#api", "#ui", "#alpha", "#zeta"}
    assert created_names.index("#alpha") < created_names.index("#zeta")
    comms.channels.set_channel_sort("#zeta", ThreadSort.LAST_MESSAGE)
    assert comms.channels.catalog.read().resolve("#zeta").created_at == 200
    comms.agents.set_activity("b", ActivityState.WORKING, "Active in zeta")
    comms.channels.set_channel_order(ChannelSort.LAST_ACTIVITY)
    assert set(names(comms)[:2]) == {"#ui", "#zeta"}
    comms.messaging.send_user_message("#alpha", "Human input", worktree=str(tmp_path))
    comms.messaging.send("b", "#zeta", "Later agent output")
    invoke_tool(comms, "comms_sort_channels", {"order": "last_user_input"})
    assert names(wire(tmp_path))[0] == "#alpha"
    assert [view.channel.name for view in comms.views.coordination_snapshot("a").channels] == [
        view.channel.name for view in wire(tmp_path).views.coordination_snapshot("b").channels
    ]
    assert comms.views.coordination_snapshot().channel_order is ChannelSort.LAST_USER_INPUT
    # Reading or re-sorting must never redefine a channel's creation time.
    assert comms.channels.catalog.read().resolve("#alpha").created_at == 300
    with patch.object(
        comms.bus.log, "_iter_log_unlocked", side_effect=AssertionError("idle rescan")
    ):
        comms.views.channel_views()


def test_membership_notices_follow_exact_membership_and_do_not_wake(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("moving", frozenset(), str(tmp_path)))
    comms.registry.declare(
        Thread(
            "peer",
            frozenset({"api"}),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    comms.channels.update_tags("moving", add=frozenset({"api"}))
    joined = comms.views.channel_history("#api")
    assert len(joined) == 1 and joined[0].membership.value == "joined"
    assert joined[0].body == "moving joined #api" and not joined[0].starts_turn
    assert "moving" not in comms.views.last_sent_timestamps()
    comms.channels.update_tags("moving", add=frozenset({"ui"}))
    comms.channels.update_tags("moving", remove=frozenset({"api"}))
    assert len(comms.views.channel_history("#api")) == 2
    comms.channels.update_tags("moving", remove=frozenset({"ui"}))
    history = wire(tmp_path).views.channel_history("#api")
    assert [message.membership.value for message in history] == ["joined", "left"]
    assert all(not message.starts_turn for message in history)
    assert not comms.views.coordination_snapshot().participants("#api")
    comms.agents.begin_turn("peer", "active-turn")
    assert [
        person.thread.name for person in comms.views.coordination_snapshot().participants("#api")
    ] == ["peer"]
    comms.owners.stop("peer")
    assert not comms.views.coordination_snapshot().participants("#api")


@pytest.mark.parametrize("order", tuple(ChannelSort))
def test_channel_pins_persist_and_partition_existing_order(tmp_path, order):
    comms = setup_wire(tmp_path)
    comms.channels.set_channel_order(order)
    original = [view.channel.name for view in comms.views.channel_views()]
    observer = wire(tmp_path)
    observer.views.channel_views()  # Populate the observer cache before the external writes.
    for channel in ("#ui", "#none"):
        result = invoke_tool(comms, "comms_pin_channel", {"name": channel, "pinned": True})
        assert result["pinned"]
    views = observer.views.coordination_snapshot().channels
    assert [view.channel.name for view in views] == (
        [name for name in original if name in {"#ui", "#none"}]
        + [name for name in original if name not in {"#ui", "#none"}]
    )
    assert all(view.to_wire()["pinned"] for view in views[:2])
    assert observer.channels.catalog.read().list_order is order
    for channel in ("#ui", "#none"):
        observer.channels.set_channel_pinned(channel, False)
    assert [view.channel.name for view in comms.views.channel_views()] == original


@pytest.mark.parametrize("order", tuple(ThreadSort))
def test_member_pins_are_persistent_channel_scoped_and_preserve_sort(tmp_path, order):
    comms = setup_wire(tmp_path)
    comms.channels.set_saved_view(
        SavedView(
            "team", ViewKind.PARTICIPANTS, ViewPredicate(AnyOfMatch, frozenset({"api", "ui"}))
        )
    )
    comms.channels.set_channel_sort("#team", order)
    original = {view.channel.name: view.members for view in comms.views.channel_views()}
    result = invoke_tool(
        comms, "comms_pin_thread", {"channel": "team", "name": "a", "pinned": True}
    )
    assert result["pinned_members"] == ["a"]
    observer = wire(tmp_path)
    views = {view.channel.name: view for view in observer.views.channel_views()}
    assert views["#team"].members == ("a", *(name for name in original["#team"] if name != "a"))
    assert views["#any"].members == original["#any"]
    assert views["#any"].pinned_members == frozenset()
    assert views["#team"].channel.order is order
    observer.channels.set_thread_pinned("team", "a", False)
    assert {view.channel.name: view.members for view in comms.views.channel_views()} == original


def test_pin_lifecycle_tracks_identity_and_membership(tmp_path):
    comms = setup_wire(tmp_path)
    comms.channels.set_saved_view(
        SavedView(
            "team", ViewKind.PARTICIPANTS, ViewPredicate(AnyOfMatch, frozenset({"api", "ui"}))
        )
    )
    comms.channels.set_channel_pinned("team", True)
    comms.channels.set_thread_pinned("team", "a", True)
    comms.channels.set_thread_pinned("#any", "a", True)
    comms.registry.register(
        replace(comms.registry.require("a"), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    comms.threads.rename_managed_thread("a", "renamed", owner_pid=os.getpid())
    observer = wire(tmp_path)
    assert observer.channels.catalog.read().pinned_threads("#team") == {"renamed"}
    assert observer.channels.catalog.read().pinned_threads("#any") == {"renamed"}
    comms.channels.update_tags("renamed", remove=frozenset({"api"}))
    team = next(view for view in observer.views.channel_views() if view.channel.name == "#team")
    assert not team.pinned_members and "renamed" not in team.members
    comms.channels.update_tags("renamed", add=frozenset({"api"}))
    team = next(view for view in observer.views.channel_views() if view.channel.name == "#team")
    assert team.pinned_members == {"renamed"} and team.members[0] == "renamed"
    comms.channels.delete_saved_view("team")
    comms.channels.set_saved_view(
        SavedView("team", ViewKind.PARTICIPANTS, ViewPredicate(AnyOfMatch, frozenset({"api"})))
    )
    assert not observer.channels.catalog.read().resolve("#team").pinned


def test_tag_view_pins_follow_rename_and_clear_on_delete(tmp_path):
    comms = setup_wire(tmp_path)
    comms.channels.set_channel_pinned("api", True)
    comms.channels.set_thread_pinned("api", "a", True)
    comms.channels.rename_tag("api", "backend")
    observer = wire(tmp_path)
    assert observer.channels.catalog.read().resolve("#backend").pinned
    assert observer.channels.catalog.read().pinned_threads("#backend") == {"a"}
    assert not observer.channels.catalog.read().pinned_threads("#api")
    comms.channels.delete_tag("backend")
    comms.channels.create_tag("backend")
    assert not observer.channels.catalog.read().resolve("#backend").pinned
    assert not observer.channels.catalog.read().pinned_threads("#backend")


def test_invalid_pins_do_not_mutate_catalog(tmp_path):
    comms = setup_wire(tmp_path)
    before = comms.channels.catalog.path.read_text()
    with pytest.raises(ValueError, match="Unknown channel"):
        comms.channels.set_channel_pinned("missing", True)
    with pytest.raises(ValueError, match="Unknown channel"):
        comms.channels.set_thread_pinned("missing", "a", True)
    with pytest.raises(ValueError, match="not a member"):
        comms.channels.set_thread_pinned("api", "b", True)
    with pytest.raises(ValueError, match="Expected.*bool"):
        invoke_tool(comms, "comms_pin_channel", {"name": "api", "pinned": "false"})
    assert comms.channels.catalog.path.read_text() == before
