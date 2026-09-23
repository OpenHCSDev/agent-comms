import json
from concurrent.futures import ProcessPoolExecutor

import pytest

from agent_comms import Comms, Thread, ThreadSort
from agent_comms.tools import invoke_tool


def add_peer(root, peer):
    Comms(root).relationships.edit("owner", "add", peer, f"Working with {peer}")


def setup_wire(tmp_path):
    comms = Comms(tmp_path / "wire")
    for name, parent in (("origin", None), ("owner", "origin"),
                         ("peer", None), ("child", "owner")):
        comms.register(Thread(name, frozenset({"team"}), str(tmp_path), parent=parent))
    return comms


def test_agent_tools_persist_only_own_explicit_edges(tmp_path, monkeypatch):
    comms = setup_wire(tmp_path)
    monkeypatch.delenv("PI_AGENT_ID", raising=False)
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    before = comms.registry.snapshot()
    receipt = invoke_tool(
        comms, "comms_collaboration", {"action": "add", "peer": "peer", "note": "Review"}
    )
    assert receipt["collaboration"]["owner"] == "owner"
    assert comms.relationships.collaborations("peer") == ()
    assert comms.full_history() == []
    assert comms.registry.snapshot() == before
    reopened = Comms(comms.root)
    original = reopened.relationships.collaborations("owner")[0]
    assert reopened.relationships.edit("owner", "add", "peer", "ignored") == original
    updated = reopened.relationships.edit("owner", "update", "peer", "Fixes")
    assert updated.note == "Fixes" and updated.created_at == original.created_at
    assert invoke_tool(comms, "comms_collaborations", {})["collaborations"][0]["note"] == "Fixes"
    invoke_tool(comms, "comms_collaboration", {"action": "remove", "peer": "peer"})
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
    snapshot = Comms(comms.root).relationships.snapshot("renamed")
    children = next(group for group in snapshot.groups if group.key == "children")
    assert children.order is ThreadSort.LAST_ACTIVITY


def test_concurrent_declaring_agents_do_not_lose_updates(tmp_path):
    comms = setup_wire(tmp_path)
    names = [f"worker-{index}" for index in range(8)]
    for name in names:
        comms.register(Thread(name, frozenset(), str(tmp_path)))
    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(add_peer, [comms.root] * len(names), names))
    assert {edge.peer for edge in comms.relationships.collaborations("owner")} == set(names)


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
    return next(group.entries for group in comms.relationships.snapshot(owner).groups
                if group.key == "collaborating")


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
    assert comms.relationships.collaborations("owner") == (original,)
    comms.relationships.edit("owner", "remove", "peer")
    new = comms.relationships.edit("owner", "add", "peer", "Explicit new task")
    assert new.peer_created != original.peer_created
    row = collaboration_rows(comms)[0]
    assert row.available and row.person.thread.created_at == new.peer_created


def test_deleted_owner_edges_are_not_purged_or_inherited_by_new_owner(tmp_path):
    comms = setup_wire(tmp_path)
    original = comms.relationships.edit("owner", "add", "peer", "Retained historical declaration")
    comms.stop("owner")
    comms.delete("owner")
    comms.relationships.edit("origin", "add", "peer", "Independent work")
    comms.register(Thread("owner", frozenset(), str(tmp_path),
                          created_at=original.owner_created + 1))
    assert comms.relationships.collaborations("owner") == ()
    assert collaboration_rows(comms) == ()
    comms.relationships.edit("owner", "add", "peer", "New owner work")
    stored = json.loads((comms.root / "relationships.json").read_text())["collaborations"]
    assert any(row["note"] == original.note and row["owner_created"] == original.owner_created
               for row in stored)
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
