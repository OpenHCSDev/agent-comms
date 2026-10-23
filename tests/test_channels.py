from agent_comms.thread_text import SavedSessionFile
from agent_comms.pi_vocabulary import HighThinkingLevel
import os
from dataclasses import replace

import pytest

from agent_comms.tools import invoke_tool
from agent_comms.tool_caller import ThreadCaller, NoThreadCaller
from agent_comms.channels import AllOfMatch, AnyOfMatch, BuiltinChannelView, ChannelMemberIndex, RetiredTagChannel, TagChannel, SavedView, ViewKind, ViewPredicate
from agent_comms.channel_targets import CommandTarget, CommsTag, DeclaredTag
from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.channel_management import (
    ArchiveThreadsTagDisposition, DeleteThreadsTagDisposition,
    DeleteExclusiveInactiveThreadsTagDisposition,
)
from agent_comms.bus import Bus
from agent_comms.comms import wire
from conftest import installed_comms
from agent_comms.field_codec import FieldCodec
from agent_comms.display_order import (
    ChannelSort, CreatedThreadSort, LastActivityThreadSort, LastMessageThreadSort, ThreadSort,
)
from agent_comms.errors import RelationViolationError
from agent_comms.registry_document import RosterVisibility
from agent_comms.thread_management import ForkSpec, GivenTags
from agent_comms.thread_status import ArchivedThreadStatus, RunningThreadStatus, StoppedThreadStatus
from agent_comms.thread_text import GivenThreadText
from agent_comms.thread_model import DeclaredModel


@pytest.mark.parametrize("channel", BuiltinChannelView.members_with(BuiltinChannelView))
def test_builtin_channel_target_decodes_to_declaring_member(channel):
    target = channel.target()
    assert FieldCodec.decode(type[BuiltinChannelView], target.name) is channel


def setup_wire(path):
    Bus.install(Bus.path_in(path))
    comms = wire(path)
    for tag in ("api", "ui"):
        comms.channels.create_tag(tag)
    for name, tags in (("a", {"api"}), ("b", {"ui"}), ("both", {"api", "ui"}), ("other", set())):
        comms.registry.declare(comms.registry.new_thread(name=name, tags=frozenset(tags), worktree=str(path)))
    return comms


def test_current_membership_cannot_create_an_unfrozen_delivery(tmp_path):
    from agent_comms.bus import PublicRecord
    from agent_comms.messages import InfoKind, Message

    comms = setup_wire(tmp_path)
    before_join = comms.messaging.send("a", "#api", "before other joined").message
    comms.channels.update_tags("other", add=frozenset({"api"}))
    addressed = comms.messaging.send("a", "#api", "frozen for other").message
    historical = comms.bus.bus.append(lambda seq, db: PublicRecord.from_message(
        Message("a", "#api", "public history is not input", InfoKind, seq=seq),
    ))
    target = CommandTarget.named("#api")
    assert [message.seq for message in comms.bus.inbox("other", target)] == [addressed.seq]
    assert comms.bus.pending_count("other", target) == 1
    assert comms.bus.pending_counts_all(("other",)) == {"other": 1}
    received = comms.bus.bus.received(comms.registry.require("other").incarnation.lookup)
    assert addressed.seq in {record.seq for record in received}
    assert not {before_join.seq, historical.seq} & {record.seq for record in received}


def test_tag_delta_and_replacement_use_the_current_registration(tmp_path):
    comms = setup_wire(tmp_path)
    changed = comms.channels.update_tags(
        "a", add=frozenset({"ui"}), remove=frozenset({"api"}),
    )
    assert changed.tags == frozenset({"ui"})
    replaced = comms.channels.replace_tags("a", frozenset({"fresh"}))
    assert replaced.tags == frozenset({"fresh"})
    assert comms.registry.require("a") == replaced
    assert "fresh" in comms.channels.catalog.read().tags
    members = {view.channel.name: view.members for view in comms.views.channel_views()}
    assert "a" in members["#fresh"]
    assert "a" not in members["#api"]
    assert "a" not in members["#ui"]


def test_channel_metadata_round_trips_without_changing_routing(tmp_path):
    comms = setup_wire(tmp_path)
    before = {view.channel.name: view.members for view in comms.views.channel_views()}
    channel = comms.channels.set_channel_metadata("api", parent="#ui", archived=True)
    assert channel.parent == "#ui" and channel.archived

    observer = wire(tmp_path)
    projected = {view.channel.name: view for view in observer.views.channel_views(RosterVisibility(archived=True))}
    assert projected["#api"].channel.parent == "#ui"
    assert projected["#api"].channel.archived
    assert {name: view.members for name, view in projected.items()} == before
    assert "#api" not in {view.channel.name for view in observer.views.channel_views()}
    assert "#api" in observer.bus.channel_declarations()

    observer.messaging.send("other", "#api", "still routable").message.message_id
    assert [message.body for message in observer.bus.inbox("a")] == ["still routable"]
    with pytest.raises(ValueError):
        observer.channels.set_channel_metadata("ui", parent="#api", archived=False)
    assert observer.bus.require_channel("#ui").parent is None

def test_tag_cohort_refuses_active_owners_before_any_registry_publication(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('inactive', frozenset({'cohort'}), str(tmp_path)), StoppedThreadStatus())
    comms.registry.declare(comms.registry.new_thread('active', frozenset({'cohort'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())), RunningThreadStatus())
    original = comms.registry.snapshot()
    catalog = comms.channels.catalog.read()
    for disposition in (ArchiveThreadsTagDisposition(), DeleteThreadsTagDisposition()):
        with pytest.raises(ValueError) as refusal:
            comms.channels.delete_tag('cohort', disposition=disposition)
        assert str(refusal.value) == disposition.confirmation('cohort')
        refusal_text = (
            'no longer stopped'
            if isinstance(disposition, ArchiveThreadsTagDisposition)
            else 'still has a live process; stop it first'
        )
        with pytest.raises(RelationViolationError):
            comms.channels.delete_tag('cohort', disposition=disposition, confirmed=True)
        assert comms.registry.snapshot() == original
        assert comms.channels.catalog.read() == catalog
        assert ProcessIdentity.capture(os.getpid()) == original.require('active').process_identity


def test_exclusive_tag_deletion_uses_original_storage_and_preserves_survivors(tmp_path):
    comms = installed_comms(tmp_path / 'wire')
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
        process = ProcessIdentity.capture(os.getpid()) if name == 'active' else NoProcess()
        comms.registry.declare(comms.registry.new_thread(name, frozenset(tags), str(tmp_path), session_file=SavedSessionFile(str(path)), process_identity=process), status)
    comms.channels.set_channel_pinned('remove', True)
    comms.channels.set_thread_pinned('keep', 'multi', True)
    original_active = comms.registry.require('active')
    original_multi = comms.registry.require('multi')
    disposition = DeleteExclusiveInactiveThreadsTagDisposition()
    with pytest.raises(ValueError):
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
    comms.registry.status('active').require_active()
    assert active.process_alive
    catalog = comms.channels.catalog.read()
    assert 'remove' not in catalog.tags
    assert '#remove' not in catalog.preferences
    assert catalog.pinned_threads('#keep') == {'multi'}
    assert not (tmp_path / 'single.jsonl').exists()
    assert not (tmp_path / 'archived.jsonl').exists()
    assert {
        name: (tmp_path / (name + '.jsonl')).read_bytes()
        for name in ('multi', 'active', 'untouched')
    } == {name: saved[name] for name in ('multi', 'active', 'untouched')}


def test_exclusive_tag_deletion_never_deletes_a_stopped_owner_that_is_still_alive(tmp_path):
    comms = wire(tmp_path / 'wire')
    original = comms.registry.new_thread('still-alive', frozenset({'remove'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
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
        comms.registry.declare(comms.registry.new_thread(f'member-{index}', frozenset({'remove', 'keep'}), str(tmp_path)), StoppedThreadStatus())
    originals = tuple(comms.registry.all_threads().values())
    current = replace(originals[0], title=GivenThreadText('new title'), task=GivenThreadText('new task'), model=DeclaredModel('selected/model'))
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
    assert 'remove' not in comms.channels.catalog.read().tags
    # An acquired cohort may predate unrelated metadata changes. Tag mutation
    # must use the locked current records, not write those older Thread values.
    comms.registry.change_tag(originals, 'keep', 'renamed')
    latest = comms.registry.require(current.name)
    assert latest.title == current.title and latest.task == current.task and latest.model == current.model
    assert latest.tags == frozenset({'renamed'})
    assert comms.registry.status(current.name) == ArchivedThreadStatus()


@pytest.mark.asyncio
async def test_new_tag_disposition_is_declared_in_original_editor_and_tool_catalog(tmp_path):
    from agent_comms.channel_management import TagDisposition
    from agent_comms.cli_commands import CliCommand
    from agent_comms.field_codec import FieldCodec, compile_decoder
    from agent_comms.tools import CommsTagsTool
    from agent_comms.native_tools import NativeRequestTools

    comms = setup_wire(tmp_path)
    action = next(action for action in CliCommand.target_catalog(comms, CliCommand.selected_targets('#api'), project=str(tmp_path))
                  if action.declaration.declared_name == 'delete-tag')
    disposition = DeleteExclusiveInactiveThreadsTagDisposition()
    editor_value = compile_decoder(TagDisposition).encode_editor(disposition)
    from agent_comms.launch_root import LaunchRoot

    with LaunchRoot.from_path(comms.root).selected():
        choices = next(field.choices for field in await action.prepare_fields()
                       if field.name == 'disposition')
    assert (disposition.label, editor_value) in choices
    edited = action.edited({'disposition': editor_value})
    assert edited.bound[0].disposition == disposition
    assert 'only inactive' in edited.confirmation
    tag_tool = next(tool for tool in NativeRequestTools.catalog() if tool['name'] == CommsTagsTool.declared_name)
    schema = tag_tool['parameters']['properties']['disposition']
    assert schema['oneOf'] == FieldCodec.value_schema(TagDisposition)['oneOf']
    assert any(member['properties'][TagDisposition.family_discriminator]['const']
               == disposition.declared_name for member in schema['oneOf'])


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
    with pytest.raises(ValueError):
        comms.messaging.send("other", "#api-and-ui", "must fail").message.message_id
    comms.channels.delete_saved_view("api-and-ui")
    assert not wire(tmp_path).channels.catalog.read().saved_views


def test_any_is_non_routable(tmp_path):
    comms = setup_wire(tmp_path)
    with pytest.raises(ValueError):
        comms.messaging.send("other", "#any", "agent send rejected").message.message_id
    with pytest.raises(ValueError):
        comms.messaging.send_user_message("#any", "user send rejected", worktree=str(tmp_path))


def test_tag_lifecycle_is_transactional_for_metadata_views_and_history(tmp_path):
    comms = setup_wire(tmp_path)
    comms.channels.create_tag("child")
    comms.channels.set_channel_sort("ui", LastActivityThreadSort)
    comms.channels.set_channel_pinned("ui", True)
    ui_created = comms.bus.require_channel("#ui").created_at
    comms.channels.set_channel_metadata("api", parent="#ui", archived=True)
    comms.channels.set_channel_metadata("child", parent="#api", archived=False)
    comms.channels.set_saved_view(
        SavedView(
            "cross-team",
            ViewKind.PARTICIPANTS,
            ViewPredicate(AllOfMatch, frozenset({"api", "ui"})),
        )
    )
    comms.messaging.send("other", "#api", "target-owned historical row").message.message_id

    before_conflict = comms.registry.snapshot()
    with pytest.raises(ValueError):
        comms.channels.rename_tag("api", "ui")
    assert comms.registry.snapshot() == before_conflict

    comms.channels.rename_tag("ui", "docs")
    observer = wire(tmp_path)
    assert observer.bus.require_channel("#api").parent == "#docs"
    assert observer.bus.require_channel("#api").archived
    docs = observer.bus.require_channel("#docs")
    assert docs.order is LastActivityThreadSort
    assert docs.created_at == ui_created
    assert docs.pinned
    assert observer.bus.require_channel("#child").parent == "#api"
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
    assert observer.bus.require_channel("#child").parent is None

    observer.channels.set_saved_view(
        SavedView(
            "cross-team",
            ViewKind.PARTICIPANTS,
            ViewPredicate(AllOfMatch, frozenset({"docs"})),
        )
    )
    restarted = wire(tmp_path)
    assert restarted.bus.require_channel("#child").parent is None
    retired = restarted.bus.require_channel("#api")
    assert isinstance(retired, RetiredTagChannel)
    assert retired.read_only and not retired.members(ChannelMemberIndex(restarted.registry.snapshot().threads.values()))
    with pytest.raises(ValueError):
        restarted.messaging.send("other", "#api", "must not route")
    with pytest.raises(ValueError):
        restarted.views.history_page(CommandTarget.named("#tpyo"))
    assert [message.body for message in list(restarted.views.history_page(CommandTarget.named("#api")).messages)] == [
        "target-owned historical row"
    ]
    assert restarted.channels.catalog.read().saved_views["cross-team"].predicate.tags == {"docs"}


def test_saved_view_names_are_reserved_for_tags_and_legacy_channels(tmp_path):
    comms = setup_wire(tmp_path)
    predicate = ViewPredicate(AnyOfMatch, frozenset({"api"}))
    with pytest.raises(ValueError):
        comms.channels.set_saved_view(SavedView("api", ViewKind.PARTICIPANTS, predicate))

    comms.channels.set_saved_view(
        SavedView(
            "reserved",
            ViewKind.PARTICIPANTS,
            predicate,
        )
    )
    before = comms.registry.snapshot()
    with pytest.raises(ValueError):
        comms.channels.create_tag("reserved")
    with pytest.raises(ValueError):
        comms.channels.update_tags("other", add=frozenset({"reserved"}))
    with pytest.raises(ValueError):
        comms.channels.rename_tag("ui", "reserved")
    assert comms.registry.snapshot() == before
    assert "reserved" not in comms.channels.catalog.read().tags


def test_all_implicit_tag_introduction_paths_honor_name_reservations(tmp_path):
    import json

    from agent_comms.cli_bridges.history import AnySession, OfflineHistory
    from agent_comms.harness_install import OpenCodeHarness

    comms = setup_wire(tmp_path)
    predicate = ViewPredicate(AnyOfMatch, frozenset({"api"}))
    comms.channels.set_saved_view(SavedView("reserved", ViewKind.PARTICIPANTS, predicate))
    before = comms.registry.snapshot()

    with pytest.raises(ValueError):
        comms.registry.declare(comms.registry.new_thread('registered', frozenset({'reserved'}), str(tmp_path)))
    with pytest.raises(ValueError):
        comms.threads.claim_thread("claimed", tags=frozenset({"reserved"}), worktree=str(tmp_path))

    source = tmp_path / "foreign.json"
    source.write_text(json.dumps({
        "info": {"id": "foreign", "title": "Foreign", "directory": str(tmp_path)},
        "messages": [{"info": {"id": "user", "sessionID": "foreign", "role": "user", "time": {"created": 1}},
                      "parts": [{"id": "user-0", "sessionID": "foreign", "messageID": "user",
                                 "type": "text", "text": "continue"}]}],
    }))

    with pytest.raises(ValueError):
        comms.threads.import_thread(
            OfflineHistory.of(OpenCodeHarness, source, AnySession()),
            name="imported",
            tags=frozenset({"reserved"}),
        )
    parent_session = tmp_path / "parent.jsonl"
    parent_session.write_text("{}\n")
    comms.registry.declare(
        comms.registry.new_thread('parent', frozenset(), str(tmp_path), session_file=SavedSessionFile(str(parent_session)))
    )
    with pytest.raises(ValueError):
        comms.threads.create(
            ForkSpec.from_task(name="child", parent="parent", project=str(tmp_path), model=DeclaredModel('test/model'), thinking_level=HighThinkingLevel, task="task", tags=GivenTags(frozenset({"reserved"}))),
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
        {"name": "api", "parent": "#ui", "archived": True}, caller=NoThreadCaller(),
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
        }, caller=NoThreadCaller(),
    )
    assert view["predicate"] == {"match": "all_of", "tags": ["api", "ui"]}
    listed = invoke_tool(comms, "comms_channels", {}, caller=NoThreadCaller())
    assert [item["name"] for item in listed["views"]] == ["cross-team"]
    invoke_tool(comms, "comms_delete_view", {"name": "cross-team"}, caller=NoThreadCaller())
    assert not invoke_tool(comms, "comms_channels", {}, caller=NoThreadCaller())["views"]


def test_tag_operations_preserve_owner_and_update_all_views(tmp_path, monkeypatch):
    comms = setup_wire(tmp_path)
    original = comms.registry.require("other")
    monkeypatch.setenv("AGENT_COMMS_THREAD", "other")
    invoke_tool(comms, "comms_tags", {"action": "create", "name": "new"}, caller=NoThreadCaller())
    assert "#new" in comms.channels.channels()
    invoke_tool(comms, "comms_thread_tags", {"add": "new,api", "remove": "ui"}, caller=ThreadCaller("other"))
    assert comms.registry.require("other") == replace(
        original, tags=frozenset({"new", "api"}), channel_scope_generation=1
    )
    invoke_tool(
        comms,
        "comms_set_view",
        {"name": "team", "kind": "participants", "match": "any_of", "tags": "api,ui"}, caller=NoThreadCaller(),
    )
    invoke_tool(comms, "comms_tags", {"action": "rename", "name": "api", "new_name": "backend"}, caller=NoThreadCaller())
    assert "backend" in comms.registry.require("a").tags
    assert wire(tmp_path).bus.require_channel("#team").tags == frozenset(
        {"backend", "ui"}
    )
    invoke_tool(comms, "comms_delete_view", {"name": "team"}, caller=NoThreadCaller())
    invoke_tool(comms, "comms_tags", {"action": "delete", "name": "backend"}, caller=NoThreadCaller())
    assert not comms.registry.require("a").tags
    assert "#ui" in comms.channels.channels()
    assert "#team" not in comms.channels.channels()
    assert invoke_tool(comms, "comms_channels", {}, caller=NoThreadCaller())["channels"]
    comms.registry.archive("a")
    assert "a" not in next(
        view.members for view in comms.views.channel_views() if view.channel.name == "#any"
    )
    assert comms.registry.status("a") == ArchivedThreadStatus()


def test_invalid_filters_and_reserved_channels(tmp_path):
    comms = setup_wire(tmp_path)
    for name in ("", "Bad Tag", "all", "any"):
        with pytest.raises(ValueError):
            comms.channels.create_tag(name)
    with pytest.raises(ValueError):
        TagChannel("empty")
    with pytest.raises(ValueError):
        TagChannel("#team", frozenset({"api", "ui"}))


def test_none_membership_and_independent_channel_order(tmp_path):
    comms = setup_wire(tmp_path)
    comms.messaging.send("a", "#none", "for untagged agents").message.message_id
    assert [message.body for message in comms.bus.inbox("other")] == ["for untagged agents"]
    assert not comms.bus.inbox("b")
    comms.channels.set_channel_sort("#api", LastMessageThreadSort)
    comms.channels.set_channel_sort("#none", LastActivityThreadSort)
    views = {view.channel.name: view for view in wire(tmp_path).views.channel_views()}
    assert views["#api"].channel.order is LastMessageThreadSort
    assert views["#none"].channel.order is LastActivityThreadSort
    assert views["#any"].channel.order is CreatedThreadSort
    assert views["#api"].members[0] == "a"
    comms.channels.update_tags("other", add=frozenset({"ui"}))
    assert not next(
        view.members for view in comms.views.channel_views() if view.channel.name == "#none"
    )


def test_membership_notices_follow_exact_membership_and_do_not_wake(tmp_path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('moving', frozenset(), str(tmp_path)))
    comms.registry.declare(
        comms.registry.new_thread('peer', frozenset({'api'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    comms.channels.update_tags("moving", add=frozenset({"api"}))
    joined = list(comms.views.history_page(CommandTarget.named("#api")).messages)
    assert len(joined) == 1 and joined[0].role.change.value == "joined"
    assert joined[0].body == "moving joined #api" and not joined[0].role.response_policy(joined[0]).starts_turn
    with comms.bus.opened_activity() as clocks:
        assert "moving" not in comms.views.last_sent_timestamps(clocks.sent.items(), registry=comms.registry.snapshot())
    comms.channels.update_tags("moving", add=frozenset({"ui"}))
    comms.channels.update_tags("moving", remove=frozenset({"api"}))
    assert len(list(comms.views.history_page(CommandTarget.named("#api")).messages)) == 2
    comms.channels.update_tags("moving", remove=frozenset({"ui"}))
    history = list(wire(tmp_path).views.history_page(CommandTarget.named("#api")).messages)
    assert [message.role.change.value for message in history] == ["joined", "left"]
    assert all(not message.role.response_policy(message).starts_turn for message in history)
    assert not comms.views.viewer_snapshot(str(tmp_path)).participants("#api")
    comms.agents.begin_turn("peer", "active-turn")
    assert [
        person.thread.name for person in comms.views.viewer_snapshot(str(tmp_path)).participants("#api")
    ] == ["peer"]
    comms.owners.stop("peer")
    assert not comms.views.viewer_snapshot(str(tmp_path)).participants("#api")


@pytest.mark.parametrize("order", ChannelSort.members_with(ChannelSort))
def test_channel_pins_persist_and_partition_existing_order(tmp_path, order):
    comms = setup_wire(tmp_path)
    comms.channels.set_channel_order(order)
    original = [view.channel.name for view in comms.views.channel_views()]
    observer = wire(tmp_path)
    observer.views.channel_views()  # Populate the observer cache before the external writes.
    for channel in ("#ui", "#none"):
        result = invoke_tool(comms, "comms_pin_channel", {"name": channel, "pinned": True}, caller=NoThreadCaller())
        assert result["pinned"]
    views = observer.views.viewer_snapshot(str(tmp_path)).channels
    assert list(views) == (
        [name for name in original if name in {"#ui", "#none"}]
        + [name for name in original if name not in {"#ui", "#none"}]
    )
    assert all(FieldCodec.encode(views[name])["pinned"] for name in list(views)[:2])
    assert observer.channels.catalog.read().list_order is order
    for channel in ("#ui", "#none"):
        observer.channels.set_channel_pinned(channel, False)
    assert [view.channel.name for view in comms.views.channel_views()] == original


@pytest.mark.parametrize("order", ThreadSort.members_with(ThreadSort))
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
        comms, "comms_pin_thread", {"channel": "team", "name": "a", "pinned": True}, caller=NoThreadCaller()
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
    assert not observer.bus.require_channel("#team").pinned


def test_tag_view_pins_follow_rename_and_clear_on_delete(tmp_path):
    comms = setup_wire(tmp_path)
    comms.channels.set_channel_pinned("api", True)
    comms.channels.set_thread_pinned("api", "a", True)
    comms.channels.rename_tag("api", "backend")
    observer = wire(tmp_path)
    assert observer.bus.require_channel("#backend").pinned
    assert observer.channels.catalog.read().pinned_threads("#backend") == {"a"}
    assert not observer.channels.catalog.read().pinned_threads("#api")
    comms.channels.delete_tag("backend")
    comms.channels.create_tag("backend")
    assert not observer.bus.require_channel("#backend").pinned
    assert not observer.channels.catalog.read().pinned_threads("#backend")


def test_invalid_pins_do_not_mutate_catalog(tmp_path):
    comms = setup_wire(tmp_path)
    before = comms.channels.catalog.path.read_text()
    with pytest.raises(ValueError):
        comms.channels.set_channel_pinned("missing", True)
    with pytest.raises(ValueError):
        comms.channels.set_thread_pinned("missing", "a", True)
    with pytest.raises(ValueError):
        comms.channels.set_thread_pinned("api", "b", True)
    with pytest.raises(ValueError):
        invoke_tool(comms, "comms_pin_channel", {"name": "api", "pinned": "false"}, caller=NoThreadCaller())
    assert comms.channels.catalog.path.read_text() == before


def test_a_declared_tag_resolves_on_a_fresh_bus_and_is_never_renamed_away_or_into(tmp_path):
    comms = wire(tmp_path)
    channel = comms.bus.require_channel(CommsTag.channel())
    assert isinstance(channel, TagChannel)
    assert CommsTag.channel() in comms.bus.channel_declarations()
    for declared in DeclaredTag.names():
        with pytest.raises(RelationViolationError, match="declared on every bus"):
            comms.channels.rename_tag(declared, "renamed")
        with pytest.raises(RelationViolationError, match="declared on every bus"):
            comms.channels.delete_tag(declared, confirmed=True)
    comms.channels.create_tag("ops")
    with pytest.raises(RelationViolationError, match="declared on every bus"):
        comms.channels.rename_tag("ops", CommsTag.declared_name)
    comms.channels.create_tag(CommsTag.declared_name)
