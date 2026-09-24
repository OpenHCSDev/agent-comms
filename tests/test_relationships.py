import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace

import pytest

from agent_comms import Comms, Thread, ThreadSort
from agent_comms.tools import invoke_tool


def add_peer(root, peer):
    Comms(root).relationships.edit("owner", "add", peer, f"Working with {peer}")


def add_shared_peer(root, owner):
    other = "peer" if owner == "owner" else "owner"
    Comms(root).relationships.edit(owner, "add", other, f"From {owner}")


def change_shared_peer(root, instruction):
    owner, action = instruction
    other = "peer" if owner == "owner" else "owner"
    Comms(root).relationships.edit(owner, action, other, "Race")


def setup_wire(tmp_path):
    comms = Comms(tmp_path / "wire")
    for name, parent in (("origin", None), ("owner", "origin"), ("peer", None), ("child", "owner")):
        comms.register(Thread(name, frozenset({"team"}), str(tmp_path), parent=parent))
    return comms


def test_agent_tools_persist_one_mutual_contact_without_sending(tmp_path, monkeypatch):
    comms = setup_wire(tmp_path)
    monkeypatch.delenv("PI_AGENT_ID", raising=False)
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    before = comms.registry.snapshot()
    receipt = invoke_tool(
        comms, "comms_collaboration", {"action": "add", "peer": "peer", "note": "Review"}
    )
    assert receipt["collaboration"]["owner"] == "owner"
    mirror = comms.relationships.collaborations("peer")[0]
    assert (mirror.owner, mirror.peer, mirror.note) == ("peer", "owner", "Review")
    assert collaboration_rows(comms, "owner")[0].target == "peer"
    assert collaboration_rows(comms, "peer")[0].target == "owner"
    assert comms.full_history() == []
    assert comms.registry.snapshot() == before
    reopened = Comms(comms.root)
    original = reopened.relationships.collaborations("owner")[0]
    assert reopened.relationships.edit("owner", "add", "peer", "ignored") == original
    updated = reopened.relationships.edit("peer", "update", "owner", "Fixes")
    assert updated.note == "Fixes" and updated.created_at == original.created_at
    assert invoke_tool(comms, "comms_collaborations", {})["collaborations"][0]["note"] == "Fixes"
    monkeypatch.setenv("AGENT_COMMS_THREAD", "peer")
    invoke_tool(comms, "comms_collaboration", {"action": "remove", "peer": "owner"})
    assert reopened.relationships.collaborations("owner") == ()
    assert reopened.relationships.collaborations("peer") == ()
    assert reopened.relationships.edit("owner", "remove", "peer") is None
    with pytest.raises(ValueError, match="does not exist"):
        reopened.relationships.edit("owner", "update", "peer")
    with pytest.raises(ValueError, match="itself"):
        reopened.relationships.edit("owner", "add", "owner")


def test_recent_contacts_survive_ack_and_do_not_infer_collaboration(tmp_path):
    comms = setup_wire(tmp_path)
    comms.send("peer", "owner", "Direct input")
    comms.send("peer", "#team", "Channel input")
    comms.send("owner", "peer", "Output")
    comms.acknowledge("owner")
    marker = (comms.root / "read_markers.json").read_bytes()
    snapshot = comms.relationships.snapshot("owner")
    groups = {group.key: group for group in snapshot.groups}
    assert [row.target for row in groups["inbound"].entries] == ["peer", "#team"]
    assert [row.target for row in groups["outbound"].entries] == ["peer"]
    assert groups["parent"].entries[0].target == "origin"
    assert groups["children"].entries[0].target == "child"
    assert groups["collaborating"].entries == ()
    assert (comms.root / "read_markers.json").read_bytes() == marker
    assert not snapshot.history_limited
    assert "Current delivery scope" in snapshot.incoming_basis


def test_alias_and_persisted_thread_sort(tmp_path):
    comms = setup_wire(tmp_path)
    comms.relationships.edit("owner", "add", "peer", "Review")
    comms.relationships.set_order("owner", "children", ThreadSort.LAST_ACTIVITY)
    comms.registry.rename("owner", "renamed")
    comms.registry.rename("peer", "reviewer")
    edge = comms.relationships.collaborations("renamed")[0]
    assert (edge.owner, edge.peer) == ("renamed", "reviewer")
    assert (
        comms.relationships.collaborations("reviewer")[0].owner,
        comms.relationships.collaborations("reviewer")[0].peer,
    ) == ("reviewer", "renamed")
    snapshot = Comms(comms.root).relationships.snapshot("renamed")
    children = next(group for group in snapshot.groups if group.key == "children")
    assert children.order is ThreadSort.LAST_ACTIVITY
    assert collaboration_rows(comms, "reviewer")[0].target == "renamed"


def test_concurrent_declaring_agents_do_not_lose_updates(tmp_path):
    comms = setup_wire(tmp_path)
    names = [f"worker-{index}" for index in range(8)]
    for name in names:
        comms.register(Thread(name, frozenset(), str(tmp_path)))
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
    comms.relationships.edit("owner", "add", "peer", "Before race")
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
    assert not comms.full_history()


def test_legacy_opposite_declarations_project_once_without_losing_notes(tmp_path):
    comms = setup_wire(tmp_path)
    first = comms.relationships.edit("owner", "add", "peer", "Owner note")
    old_opposite = replace(
        first,
        owner="peer",
        peer="owner",
        owner_created=first.peer_created,
        peer_created=first.owner_created,
        note="Peer note",
        updated_at=first.updated_at + 1,
    )
    path = comms.root / "relationships.json"
    state = json.loads(path.read_text())
    state["collaborations"].append(asdict(old_opposite))
    path.write_text(json.dumps(state))
    for owner, other in (("owner", "peer"), ("peer", "owner")):
        assert len(comms.relationships.collaborations(owner)) == 1
        row = collaboration_rows(comms, owner)[0]
        assert row.target == other and row.detail == "Owner note\nPeer note"
    comms.relationships.edit("owner", "add", "child", "Other work")
    assert len(json.loads(path.read_text())["collaborations"]) == 3
    comms.relationships.edit("peer", "remove", "owner")
    assert collaboration_rows(comms, "owner")[0].target == "child"
    assert collaboration_rows(comms, "peer") == ()
    assert len(json.loads(path.read_text())["collaborations"]) == 1


def test_bounded_window_reports_omitted_history_and_reuses_tail(tmp_path, monkeypatch):
    comms = setup_wire(tmp_path)
    for index in range(5):
        comms.send("peer", "owner", f"Input {index}")
    service = comms.relationships
    monkeypatch.setattr(service, "RECENT_MESSAGES", 2)
    first = service.snapshot("owner")
    assert first.history_limited and first.history_messages == 2
    cached = service._recent
    service.snapshot("owner")
    assert service._recent is cached
    comms.send("owner", "peer", "New output")
    assert service.snapshot("owner").groups[1].entries[0].detail.endswith("New output")


def collaboration_rows(comms, owner="owner"):
    return next(
        group.entries
        for group in comms.relationships.snapshot(owner).groups
        if group.key == "collaborating"
    )


def test_deleted_peer_survives_unrelated_edit_and_explicit_remove(tmp_path):
    comms = setup_wire(tmp_path)
    original = comms.relationships.edit("owner", "add", "peer", "Unfinished review notes")
    comms.stop("peer")
    comms.delete("peer")

    missing = collaboration_rows(comms)[0]
    assert missing.target == "peer", "Deleted identity must remain copyable"
    assert missing.available is False and missing.person is None
    assert missing.detail == original.note
    assert comms.relationships.collaborations("owner") == (original,)

    # The reported bug: an edit of a DIFFERENT edge must not garbage-collect
    # missing peers and their notes out of the durable state.
    comms.relationships.edit("owner", "add", "child", "Another task")
    comms.relationships.edit("owner", "update", "child", "Updated other task")
    reopened = Comms(comms.root)
    persisted = {edge.peer: edge for edge in reopened.relationships.collaborations("owner")}
    assert persisted["peer"] == original
    missing = next(row for row in collaboration_rows(reopened) if row.target == "peer")
    assert not missing.available and missing.detail == original.note
    assert reopened.relationships.edit("owner", "remove", "peer") is None
    assert reopened.relationships.edit("owner", "remove", "peer") is None
    assert [edge.peer for edge in reopened.relationships.collaborations("owner")] == ["child"]


def test_surviving_peer_can_end_unavailable_collaboration(tmp_path):
    comms = setup_wire(tmp_path)
    comms.relationships.edit("owner", "add", "peer", "Work to remember")
    comms.stop("owner")
    comms.delete("owner")
    row = collaboration_rows(comms, "peer")[0]
    assert (row.target, row.available, row.detail) == ("owner", False, "Work to remember")
    comms.relationships.edit("peer", "remove", "owner")
    assert collaboration_rows(comms, "peer") == ()
    assert json.loads((comms.root / "relationships.json").read_text())["collaborations"] == []


def test_reused_peer_name_does_not_rebind_or_overwrite_historical_work(tmp_path):
    comms = setup_wire(tmp_path)
    old_peer = comms.registry.require("peer")
    original = comms.relationships.edit("owner", "add", "peer", "Old incarnation's task")
    comms.stop("peer")
    comms.delete("peer")
    comms.register(Thread("peer", frozenset(), str(tmp_path), created_at=old_peer.created_at + 1))

    row = collaboration_rows(comms)[0]
    assert row.target == "peer" and row.person is None and not row.available
    for action in ("add", "update"):
        with pytest.raises(ValueError, match="identity was replaced"):
            comms.relationships.edit("owner", action, "peer", "Must not overwrite old task")
    assert collaboration_rows(comms, "peer") == ()
    # The new thread cannot erase its predecessor's note from the old peer list.
    comms.relationships.edit("peer", "remove", "owner")
    assert comms.relationships.collaborations("owner") == (original,)
    comms.relationships.edit("owner", "remove", "peer")
    new = comms.relationships.edit("owner", "add", "peer", "Explicit new task")
    assert new.peer_created != original.peer_created
    row = collaboration_rows(comms)[0]
    assert row.available and row.person.thread.created_at == new.peer_created
    assert collaboration_rows(comms, "peer")[0].target == "owner"


def test_deleted_owner_edges_are_not_purged_or_inherited_by_new_owner(tmp_path):
    comms = setup_wire(tmp_path)
    original = comms.relationships.edit("owner", "add", "peer", "Retained historical declaration")
    comms.stop("owner")
    comms.delete("owner")
    comms.relationships.edit("origin", "add", "peer", "Independent work")
    comms.register(
        Thread("owner", frozenset(), str(tmp_path), created_at=original.owner_created + 1)
    )
    assert comms.relationships.collaborations("owner") == ()
    assert collaboration_rows(comms) == ()
    old_row = next(row for row in collaboration_rows(comms, "peer") if row.target == "owner")
    assert not old_row.available and old_row.detail == original.note
    comms.relationships.edit("owner", "add", "peer", "New owner work")
    stored = json.loads((comms.root / "relationships.json").read_text())["collaborations"]
    assert any(
        row["note"] == original.note and row["owner_created"] == original.owner_created
        for row in stored
    )
    assert [edge.note for edge in comms.relationships.collaborations("owner")] == ["New owner work"]


def test_live_alias_resolves_but_deleted_alias_does_not_erase_note(tmp_path, monkeypatch):
    comms = setup_wire(tmp_path)
    comms.relationships.edit("owner", "add", "peer", "Review before rename")
    monkeypatch.delenv("PI_AGENT_ID", raising=False)
    monkeypatch.setenv("AGENT_COMMS_THREAD", "peer")
    comms.rename_self("reviewer")
    assert collaboration_rows(comms)[0].target == "reviewer"
    comms.stop("reviewer")
    comms.delete("reviewer")
    comms.relationships.edit("owner", "add", "child")
    row = next(row for row in collaboration_rows(comms) if not row.available)
    assert row.target == "peer" and row.detail == "Review before rename"
