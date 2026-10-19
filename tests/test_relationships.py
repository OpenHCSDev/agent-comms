import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

import pytest

from agent_comms.registry_document import RosterVisibility
from agent_comms.relationships import (
    ChildrenRelationshipGroup, CollaboratingRelationshipGroup, InboundRelationshipGroup,
    OutboundRelationshipGroup, ParentRelationshipGroup, RelationshipEdit,
)
from agent_comms.comms import Comms
from agent_comms.display_order import LastActivityThreadSort
from agent_comms.threads import Thread
from agent_comms.tools import invoke_tool


def add_peer(root, peer):
    Comms(root).relationships.edit(
        "owner", RelationshipEdit.member_named("add"), peer, f"Working with {peer}"
    )


def add_shared_peer(root, owner):
    other = "peer" if owner == "owner" else "owner"
    Comms(root).relationships.edit(owner, RelationshipEdit.member_named("add"), other, f"From {owner}")


def change_shared_peer(root, instruction):
    owner, action = instruction
    other = "peer" if owner == "owner" else "owner"
    Comms(root).relationships.edit(owner, RelationshipEdit.member_named(action), other, "Race")


def setup_wire(tmp_path):
    comms = Comms(tmp_path / "wire")
    for name, parent in (("origin", None), ("owner", "origin"), ("peer", None), ("child", "owner")):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path), parent=parent))
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
    assert list(comms.views.full_history_page().messages) == []
    assert comms.registry.snapshot() == before
    reopened = Comms(comms.root)
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
    with pytest.raises(ValueError, match="does not exist"):
        reopened.relationships.edit("owner", RelationshipEdit.member_named("update"), "peer")
    with pytest.raises(ValueError, match="itself"):
        reopened.relationships.edit("owner", RelationshipEdit.member_named("add"), "owner")


def test_recent_contacts_survive_ack_and_do_not_infer_collaboration(tmp_path):
    comms = setup_wire(tmp_path)
    comms.messaging.send("peer", "owner", "Direct input").message.message_id
    comms.messaging.send("peer", "#team", "Channel input").message.message_id
    comms.messaging.send("owner", "peer", "Output").message.message_id
    messages = comms.bus.inbox("owner")
    comms.messaging.acknowledge("owner", tuple(message.reference for message in messages))
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
    snapshot = Comms(comms.root).relationships.snapshot("renamed")
    children = snapshot.groups_by_type[ChildrenRelationshipGroup]
    assert children.order is LastActivityThreadSort
    assert collaboration_rows(comms, "reviewer")[0].target == "renamed"


def test_concurrent_declaring_agents_do_not_lose_updates(tmp_path):
    comms = setup_wire(tmp_path)
    names = [f"worker-{index}" for index in range(8)]
    for name in names:
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
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
    assert not list(comms.views.full_history_page().messages)


def test_bounded_window_reports_omitted_history_and_reuses_tail(tmp_path, monkeypatch):
    comms = setup_wire(tmp_path)
    for index in range(5):
        comms.messaging.send("peer", "owner", f"Input {index}").message.message_id
    service = comms.relationships
    monkeypatch.setattr(service, "RECENT_MESSAGES", 2)
    first = service.snapshot("owner")
    assert first.history_limited and first.history_messages == 2
    cached = service._recent
    service.snapshot("owner")
    assert service._recent is cached
    comms.messaging.send("owner", "peer", "New output").message.message_id
    assert service.snapshot("owner").groups[1].entries["peer"].detail.endswith("New output")


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
    assert missing.available is False and missing.person is None
    assert missing.detail == original.note
    assert comms.relationships.collaborations("owner") == (original,)

    # The reported bug: an edit of a DIFFERENT edge must not garbage-collect
    # missing peers and their notes out of the durable state.
    comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "child", "Another task")
    comms.relationships.edit(
        "owner", RelationshipEdit.member_named("update"), "child", "Updated other task"
    )
    reopened = Comms(comms.root)
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
        comms.registry.new_thread('peer', frozenset(), str(tmp_path), created_at=old_peer.created_at + 1)
    )

    row = collaboration_rows(comms)[0]
    assert row.target == "peer" and row.person is None and not row.available
    for action in ("add", "update"):
        with pytest.raises(ValueError, match="identity was replaced"):
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
    assert row.available and row.person.thread.created_at == new.peer_created
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
        comms.registry.new_thread('owner', frozenset(), str(tmp_path), created_at=original.owner_created + 1)
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
        and visible.person.thread.incarnation == comms.registry.require("renamed-peer").incarnation
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
    assert not unavailable.available and unavailable.person is None
    assert "Retained explicit contact" in unavailable.detail


def test_revision_changes_when_a_relationship_or_a_thread_does(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_comms.history_views.time.time", lambda: 1_000)
    comms = setup_wire(tmp_path)
    start = comms.relationships.revision()
    assert comms.relationships.revision() == start
    comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "peer", "Review")
    edited = comms.relationships.revision()
    assert edited != start
    comms.registry.rename("peer", "reviewer")
    assert comms.relationships.revision() != edited


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
        reused = replace(earlier.person, thread=replace(
            earlier.person.thread, created_at=earlier.person.thread.created_at + 1
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
        reopened = Comms(comms.root).relationships.snapshot("owner")
        assert FieldCodec.decode(type(reopened), FieldCodec.encode(reopened)) == reopened
    finally:
        RelationshipGroup.__registry__.pop(SortedInboundRelationshipGroup.declared_name)
