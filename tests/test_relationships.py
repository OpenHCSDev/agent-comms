
from agent_comms.repo import Checkout
from bus_history import read_history
import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from agent_comms.registry_document import InvalidRegistryDocument, RosterVisibility, ThreadRows
from agent_comms.relationships import (
    ChildrenRelationshipGroup, CollaboratingRelationshipGroup, InboundRelationshipGroup,
    NamedEntrySource, OutboundRelationshipGroup, ParentRelationshipGroup,
    RelationshipContext, RelationshipEdit, RelationshipEntry, RelationshipEntrySource,
    RelationshipGroup, RelationshipInputs, SortableRelationshipGroup,
)
from agent_comms.comms import Comms, wire
from conftest import installed_comms
from agent_comms.display_order import LastActivityThreadSort, ThreadSort
from agent_comms.field_codec import FieldCodec
from agent_comms.bus import PublicRecord
from agent_comms.messages import InfoKind, Message, NoticeRole
from agent_comms.routing import DeliveryScope
from agent_comms.thread_text import ForkedParent, NoParent
from agent_comms.tools import invoke_tool
from agent_comms.threads import Thread


def add_peer(root, peer):
    wire(root).relationships.edit(
        "owner", RelationshipEdit.member_named("add"), peer, f"Working with {peer}"
    )


def add_shared_peer(root, owner):
    other = "peer" if owner == "owner" else "owner"
    wire(root).relationships.edit(owner, RelationshipEdit.member_named("add"), other, f"From {owner}")


def change_shared_peer(root, instruction):
    owner, action = instruction
    other = "peer" if owner == "owner" else "owner"
    wire(root).relationships.edit(owner, RelationshipEdit.member_named(action), other, "Race")


def setup_wire(tmp_path: Path) -> Comms:
    comms = installed_comms(tmp_path / "wire")
    for name, parent in (("origin", NoParent()), ("owner", ForkedParent("origin")), ("peer", NoParent()),
                         ("child", ForkedParent("owner"))):
        comms.registry.declare(Thread(name, frozenset({'team'}), Checkout(tmp_path), parent=parent))
    return comms


def test_agent_tools_persist_one_mutual_contact_without_sending(tmp_path, monkeypatch):
    from agent_comms.tool_caller import ThreadCaller
    comms = setup_wire(tmp_path)
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    before = comms.registry.snapshot()
    acknowledgement = invoke_tool(
        comms, "comms_collaboration", {"action": "add", "peer": "peer", "note": "Review"}, caller=ThreadCaller("owner")
    )
    assert acknowledgement["collaboration"]["owner"] == "owner"
    mirror = comms.relationships.collaborations("peer")[0]
    assert (mirror.owner, mirror.peer, mirror.note) == ("peer", "owner", "Review")
    assert collaboration_rows(comms, "owner")[0].target == "peer"
    assert collaboration_rows(comms, "peer")[0].target == "owner"
    assert read_history(comms) == []
    assert comms.registry.snapshot() == before
    reopened = wire(comms.root)
    original = reopened.relationships.collaborations("owner")[0]
    assert (
        reopened.relationships.edit("owner", RelationshipEdit.member_named("add"), "peer", "ignored")
        == original
    )
    updated = reopened.relationships.edit(
        "peer", RelationshipEdit.member_named("update"), "owner", "Fixes"
    )
    assert updated.note == "Fixes" and updated.created_at == original.created_at
    assert invoke_tool(comms, "comms_collaborations", {}, caller=ThreadCaller("owner"))["collaborations"][0]["note"] == "Fixes"
    monkeypatch.setenv("AGENT_COMMS_THREAD", "peer")
    invoke_tool(comms, "comms_collaboration", {"action": "remove", "peer": "owner"}, caller=ThreadCaller("peer"))
    assert reopened.relationships.collaborations("owner") == ()
    assert reopened.relationships.collaborations("peer") == ()
    assert reopened.relationships.edit("owner", RelationshipEdit.member_named("remove"), "peer") is None
    with pytest.raises(ValueError):
        reopened.relationships.edit("owner", RelationshipEdit.member_named("update"), "peer")
    with pytest.raises(ValueError):
        reopened.relationships.edit("owner", RelationshipEdit.member_named("add"), "owner")


def test_recent_contacts_survive_ack_and_do_not_infer_collaboration(tmp_path):
    comms = setup_wire(tmp_path)
    comms.messaging.send("peer", "owner", "Direct input").message.message_id
    comms.messaging.send("peer", "#team", "Channel input").message.message_id
    comms.messaging.send("owner", "peer", "Output").message.message_id
    messages = comms.bus.inbox("owner").page.messages
    comms.bus.mark_read_through("owner", messages[-1].seq)
    pending = comms.bus.pending_count("owner")
    snapshot = comms.relationships.snapshot("owner")
    groups = snapshot.groups_by_type
    assert list(groups[InboundRelationshipGroup].entries) == ["peer", "#team"]
    assert list(groups[OutboundRelationshipGroup].entries) == ["peer"]
    assert groups[ParentRelationshipGroup].entries["origin"].target == "origin"
    assert groups[ChildrenRelationshipGroup].entries["child"].target == "child"
    assert groups[CollaboratingRelationshipGroup].entries == {}
    assert comms.bus.pending_count("owner") == pending
    assert not snapshot.history_limited
    assert "Current delivery scope" in snapshot.incoming_basis


def test_alias_and_persisted_thread_sort(tmp_path):
    comms = setup_wire(tmp_path)
    comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "peer", "Review")
    comms.relationships.set_order("owner", ChildrenRelationshipGroup, LastActivityThreadSort)
    comms.registry.rename("owner", "renamed")
    comms.registry.rename("peer", "reviewer")
    edge = comms.relationships.collaborations("renamed")[0]
    assert (edge.owner, edge.peer) == ("renamed", "reviewer")
    assert (
        comms.relationships.collaborations("reviewer")[0].owner,
        comms.relationships.collaborations("reviewer")[0].peer,
    ) == ("reviewer", "renamed")
    snapshot = wire(comms.root).relationships.snapshot("renamed")
    children = snapshot.groups_by_type[ChildrenRelationshipGroup]
    assert children.order is LastActivityThreadSort
    assert collaboration_rows(comms, "reviewer")[0].target == "renamed"


def test_recent_relationships_keep_the_global_cut_and_exact_preview(tmp_path):
    comms = setup_wire(tmp_path)
    bus = comms.bus.bus
    bus.append(lambda seq, db: PublicRecord.from_message(
        Message("peer", "owner", "evicted relevant input", InfoKind, seq=seq),
    ))
    for _ in range(comms.relationships.RECENT_MESSAGES):
        bus.append(lambda seq, db: PublicRecord.from_message(
            Message("peer", "origin", "unrelated global row", InfoKind, seq=seq),
        ))
    snapshot = comms.relationships.snapshot("owner")
    assert snapshot.history_limited and snapshot.history_messages == comms.relationships.RECENT_MESSAGES
    assert not snapshot.groups_by_type[InboundRelationshipGroup].entries

    body = "preview " * 50
    latest = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("peer", "owner", body, InfoKind, seq=seq),
    ))
    bus.append(lambda seq, db: PublicRecord.from_message(
        Message("peer", "owner", "not a conversation", InfoKind, seq=seq, role=NoticeRole()),
    ))
    snapshot = comms.relationships.snapshot("owner")
    entry = snapshot.groups_by_type[InboundRelationshipGroup].entries["peer"]
    assert entry.sequence == latest.seq and entry.timestamp == latest.timestamp
    assert entry.detail == f"peer → owner\n{body[:240]}"
    assert snapshot.history_limited and snapshot.history_messages == comms.relationships.RECENT_MESSAGES


@pytest.mark.parametrize("order", ThreadSort.members_with(ThreadSort))
def test_group_selection_hydrates_only_selected_threads_on_its_captured_cut(
    tmp_path: Path, order: type[ThreadSort],
):
    comms = setup_wire(tmp_path)
    comms.registry.declare(Thread("unselected", frozenset(), Checkout(tmp_path)))
    comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "peer", "Real contact")
    captured = comms.registry.snapshot()
    raw = captured.threads.to_wire()
    raw["unselected"][FieldCodec.field_plan(Thread, "model").key] = 123
    registry = replace(captured, threads=ThreadRows.from_wire(raw))
    thread = registry.require("owner")
    contacts, _ = comms.relationships._goal_contacts(registry, thread)
    orders = {group: order for group in SortableRelationshipGroup.members_with(SortableRelationshipGroup)}
    sent = order.sent_timestamps(comms.views, registry=registry)
    inputs = RelationshipInputs(
        registry, thread, (),
        DeliveryScope.capture(thread.name, registry, comms.views.channels.catalog.read()),
        comms.relationships.store.read().resolved(registry).collaborations, contacts,
        MappingProxyType(orders), MappingProxyType(sent),
    )
    original_orders, original_sent = dict(orders), dict(sent)
    groups = {type(group): group for group in RelationshipGroup.capture(inputs, comms.views)}
    for group_type, target in ((ParentRelationshipGroup, "origin"),
                               (ChildrenRelationshipGroup, "child"),
                               (CollaboratingRelationshipGroup, "peer")):
        entry = groups[group_type].entries[target]
        assert entry.available and entry.person.require_present().thread is registry.require(target)
    selected = {person.thread.name for group in groups.values()
                for entry in group.entries.values() for person in entry.person.views()}
    assert selected == {"origin", "child", "peer"}
    assert inputs.orders == original_orders and inputs.sent == original_sent
    assert all(selected.order is order for group in groups.values() for selected in group.selectable())
    with pytest.raises(InvalidRegistryDocument):
        registry.require("unselected")


def test_concurrent_declaring_agents_do_not_lose_updates(tmp_path):
    comms = setup_wire(tmp_path)
    names = [f"worker-{index}" for index in range(8)]
    for name in names:
        comms.registry.declare(Thread(name, frozenset(), Checkout(tmp_path)))
    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(add_peer, [comms.root] * len(names), names))
    assert {edge.peer for edge in comms.relationships.collaborations("owner")} == set(names)
    for name in names:
        assert comms.relationships.collaborations(name)[0].peer == "owner"


def test_concurrent_opposite_adds_create_one_shared_pair(tmp_path):
    comms = setup_wire(tmp_path)
    with ProcessPoolExecutor(max_workers=2) as pool:
        list(pool.map(add_shared_peer, [comms.root] * 2, ("owner", "peer")))
    raw = json.loads((comms.root / "relationships.json").read_text())["collaborations"]
    assert len(raw) == 1
    left = comms.relationships.collaborations("owner")
    right = comms.relationships.collaborations("peer")
    assert len(left) == len(right) == 1
    assert left[0].note == right[0].note
    assert left[0].created_at == right[0].created_at
    assert {left[0].note} <= {"From owner", "From peer"}


def test_concurrent_add_remove_never_leaves_one_sided_contact(tmp_path):
    comms = setup_wire(tmp_path)
    comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "peer", "Before race")
    for _ in range(4):
        with ProcessPoolExecutor(max_workers=2) as pool:
            list(
                pool.map(
                    change_shared_peer, [comms.root] * 2, (("owner", "add"), ("peer", "remove"))
                )
            )
        left = comms.relationships.collaborations("owner")
        right = comms.relationships.collaborations("peer")
        assert len(left) == len(right) <= 1
        assert bool(collaboration_rows(comms, "owner")) == bool(left)
        assert bool(collaboration_rows(comms, "peer")) == bool(right)
        assert len(
            json.loads((comms.root / "relationships.json").read_text())["collaborations"]
        ) == len(left)
    assert not read_history(comms)


def collaboration_rows(comms, owner="owner"):
    return tuple(comms.relationships.snapshot(owner).groups_by_type[CollaboratingRelationshipGroup].entries.values())


def test_deleted_peer_survives_unrelated_edit_and_explicit_remove(tmp_path):
    comms = setup_wire(tmp_path)
    original = comms.relationships.edit(
        "owner", RelationshipEdit.member_named("add"), "peer", "Unfinished review notes"
    )
    comms.owners.stop("peer")
    comms.registry.remove("peer")

    missing = collaboration_rows(comms)[0]
    assert missing.target == "peer", "Deleted identity must remain copyable"
    assert missing.available is False and missing.person.views() == ()
    snapshot = comms.relationships.snapshot("owner")
    assert FieldCodec.decode(type(snapshot), FieldCodec.encode(snapshot)) == snapshot
    assert missing.detail == original.note
    assert comms.relationships.collaborations("owner") == (original,)

    # The reported bug: an edit of a DIFFERENT edge must not garbage-collect
    # missing peers and their notes out of the durable state.
    comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "child", "Another task")
    comms.relationships.edit(
        "owner", RelationshipEdit.member_named("update"), "child", "Updated other task"
    )
    reopened = wire(comms.root)
    persisted = {edge.peer: edge for edge in reopened.relationships.collaborations("owner")}
    assert persisted["peer"] == original
    missing = next(row for row in collaboration_rows(reopened) if row.target == "peer")
    assert not missing.available and missing.detail == original.note
    assert reopened.relationships.edit("owner", RelationshipEdit.member_named("remove"), "peer") is None
    assert reopened.relationships.edit("owner", RelationshipEdit.member_named("remove"), "peer") is None
    assert [edge.peer for edge in reopened.relationships.collaborations("owner")] == ["child"]


def test_surviving_peer_can_end_unavailable_collaboration(tmp_path):
    comms = setup_wire(tmp_path)
    comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "peer", "Work to remember")
    comms.owners.stop("owner")
    comms.registry.remove("owner")
    row = collaboration_rows(comms, "peer")[0]
    assert (row.target, row.available, row.detail) == ("owner", False, "Work to remember")
    comms.relationships.edit("peer", RelationshipEdit.member_named("remove"), "owner")
    assert collaboration_rows(comms, "peer") == ()
    assert json.loads((comms.root / "relationships.json").read_text())["collaborations"] == []


def test_reused_peer_name_does_not_rebind_or_overwrite_historical_work(tmp_path):
    comms = setup_wire(tmp_path)
    old_peer = comms.registry.require("peer")
    original = comms.relationships.edit(
        "owner", RelationshipEdit.member_named("add"), "peer", "Old incarnation's task"
    )
    comms.owners.stop("peer")
    comms.registry.remove("peer")
    comms.registry.declare(
        Thread('peer', frozenset(), Checkout(tmp_path), created_at=old_peer.created_at + 1)
    )

    row = collaboration_rows(comms)[0]
    assert row.target == "peer" and row.person.views() == () and not row.available
    for action in ("add", "update"):
        with pytest.raises(ValueError):
            comms.relationships.edit(
                "owner", RelationshipEdit.member_named(action), "peer", "Must not overwrite old task"
            )
    assert collaboration_rows(comms, "peer") == ()
    # The new thread cannot erase its predecessor's note from the old peer list.
    comms.relationships.edit("peer", RelationshipEdit.member_named("remove"), "owner")
    assert comms.relationships.collaborations("owner") == (original,)
    comms.relationships.edit("owner", RelationshipEdit.member_named("remove"), "peer")
    new = comms.relationships.edit(
        "owner", RelationshipEdit.member_named("add"), "peer", "Explicit new task"
    )
    assert new.peer_created != original.peer_created
    row = collaboration_rows(comms)[0]
    assert row.available and row.person.require_present().thread.birth == new.peer_created
    assert collaboration_rows(comms, "peer")[0].target == "owner"


def test_deleted_owner_edges_are_not_purged_or_inherited_by_new_owner(tmp_path):
    comms = setup_wire(tmp_path)
    original = comms.relationships.edit(
        "owner", RelationshipEdit.member_named("add"), "peer", "Retained historical declaration"
    )
    comms.owners.stop("owner")
    comms.registry.remove("owner")
    comms.relationships.edit("origin", RelationshipEdit.member_named("add"), "peer", "Independent work")
    comms.registry.declare(
        Thread('owner', frozenset(), Checkout(tmp_path), created_at=original.owner_created + 1)
    )
    assert comms.relationships.collaborations("owner") == ()
    assert collaboration_rows(comms) == ()
    old_row = next(row for row in collaboration_rows(comms, "peer") if row.target == "owner")
    assert not old_row.available and old_row.detail == original.note
    comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "peer", "New owner work")
    stored = json.loads((comms.root / "relationships.json").read_text())["collaborations"]
    assert any(
        row["note"] == original.note and row["owner_created"] == original.owner_created
        for row in stored
    )
    assert [edge.note for edge in comms.relationships.collaborations("owner")] == ["New owner work"]


def test_live_alias_resolves_but_deleted_alias_does_not_erase_note(tmp_path, monkeypatch):
    comms = setup_wire(tmp_path)
    comms.relationships.edit(
        "owner", RelationshipEdit.member_named("add"), "peer", "Review before rename"
    )
    monkeypatch.setenv("AGENT_COMMS_THREAD", "peer")
    from agent_comms.tool_caller import ThreadCaller

    comms.threads.rename_self("reviewer", caller=ThreadCaller("peer"))
    assert collaboration_rows(comms)[0].target == "reviewer"
    comms.owners.stop("reviewer")
    comms.registry.remove("reviewer")
    comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "child")
    row = next(row for row in collaboration_rows(comms) if not row.available)
    assert row.target == "peer" and row.detail == "Review before rename"


def test_retained_alias_contact_without_projected_person_is_unavailable(tmp_path):
    from agent_comms.goal_actions import SetGoalAction

    comms = setup_wire(tmp_path)
    peer = comms.registry.require("peer")
    comms.relationships.edit(
        "owner", RelationshipEdit.member_named("add"), "peer", "Retained explicit contact"
    )
    comms.goals.update_goal("owner", SetGoalAction(text="Review with @peer"))
    comms.registry.rename("peer", "renamed-peer")
    visible = collaboration_rows(comms)[0]
    assert (
        visible.available
        and visible.person.require_present().thread.incarnation == comms.registry.require("renamed-peer").incarnation
    )
    assert visible.target == "renamed-peer"
    assert visible.sources == ("explicit", "goal_mention")

    # Deletion retains the exact current incarnation, but the real roster
    # excludes it even when stopped/archived entries were requested.
    comms.owners.stop("renamed-peer")
    comms.registry.begin_delete("renamed-peer")
    retained = comms.registry.require("renamed-peer")
    assert retained.incarnation.current(comms.registry.snapshot())
    assert retained.name not in {
        view.thread.name for view in comms.views.thread_views(RosterVisibility(archived=True))
    }
    unavailable = collaboration_rows(comms)[0]
    assert unavailable.target == "renamed-peer"
    assert unavailable.sources == ("explicit", "goal_mention")
    assert not unavailable.available and unavailable.person.views() == ()
    assert "Retained explicit contact" in unavailable.detail


def test_new_group_supplies_snapshot_choices_defaults_and_persistence(tmp_path):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.relationships import (
        InboundRelationshipGroup, RelationshipGroup, SortableRelationshipGroup,
    )

    class SortedInboundRelationshipGroup(SortableRelationshipGroup, InboundRelationshipGroup):
        title = "Sorted inbound"
        default_order = LastActivityThreadSort

    try:
        comms = setup_wire(tmp_path)
        comms.messaging.send("peer", "owner", "Review").message.message_id
        snapshot = comms.relationships.snapshot("owner")
        group = next(group for group in snapshot.sortable_groups if type(group) is SortedInboundRelationshipGroup)
        assert FieldCodec.encode(group)["key"] == "sorted_inbound"
        assert group.order is LastActivityThreadSort
        assert group.entries["peer"].target == "peer"
        earlier = group.entries["peer"]
        assert snapshot.groups_by_type[SortedInboundRelationshipGroup] is group
        assert snapshot.current_entry(SortedInboundRelationshipGroup, earlier) is earlier
        # The inbound capability is not the exact sorted-inbound group identity.
        selected = replace(snapshot, groups=(group,))
        assert selected.current_entry(InboundRelationshipGroup, earlier) is None
        assert selected.current_entry(
            SortedInboundRelationshipGroup, replace(earlier, kind="channel")
        ) is None
        person = earlier.person.require_present()
        reused = replace(person, thread=replace(
            person.thread, created_at=person.thread.created_at + 1
        ))
        assert selected.current_entry(
            SortedInboundRelationshipGroup, replace(earlier, person=reused)
        ) is None
        unavailable = replace(selected, groups=(replace(
            group, entries={earlier.target: replace(earlier, available=False)}
        ),))
        assert unavailable.current_entry(SortedInboundRelationshipGroup, earlier) is None
        assert all(group.order is None for group in snapshot.groups if not group.selectable())
        comms.relationships.set_order("owner", SortedInboundRelationshipGroup, LastActivityThreadSort)
        encoded = json.loads(comms.relationships.store.path.read_text())
        assert encoded["orders"][0]["group"] == "sorted_inbound"
        reopened = wire(comms.root).relationships.snapshot("owner")
        assert FieldCodec.decode(type(reopened), FieldCodec.encode(reopened)) == reopened
    finally:
        RelationshipGroup.__registry__.pop(SortedInboundRelationshipGroup.declared_name)


def test_new_group_retains_its_one_original_source_through_complete_presentation(tmp_path: Path):
    comms = setup_wire(tmp_path)
    selections: list[RelationshipInputs] = []
    originals: list[NamedEntrySource] = []
    built: list[NamedEntrySource] = []

    class CapturedEntrySource(NamedEntrySource):
        __slots__ = ()

        def entry(self, context: RelationshipContext) -> RelationshipEntry:
            built.append(self)
            assert context.registry is selections[0].registry
            assert context.people[self.name].thread is context.registry.require(self.name)
            with pytest.raises(TypeError):
                context.people["unexpected"] = context.people[self.name]
            return super().entry(context)

    class CapturedRelationshipGroup(RelationshipGroup):
        title = "One original source"

        @classmethod
        def sources(cls, inputs: RelationshipInputs) -> tuple[RelationshipEntrySource, ...]:
            selections.append(inputs)
            assert len(selections) == 1
            source = CapturedEntrySource(
                CapturedEntrySource.canonical_target("peer", inputs.registry),
            )
            originals.append(source)
            return (source,)

    try:
        snapshot = comms.relationships.snapshot("owner")
        group = snapshot.groups_by_type[CapturedRelationshipGroup]
        assert len(selections) == len(originals) == len(built) == 1
        assert built[0] is originals[0]
        entry = group.entries["peer"]
        assert entry.person.require_present().thread is selections[0].registry.require("peer")
        assert entry.available and entry.target == originals[0].name
    finally:
        RelationshipGroup.__registry__.pop(CapturedRelationshipGroup.declared_name)
