"""Goal mentions are incarnation-bound contact awareness, not accepted work."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_comms import Comms, Thread
from agent_comms.declarations import ThreadRole
from agent_comms.tools import invoke_tool


def _wire(tmp_path: Path) -> Comms:
    comms = Comms(tmp_path / "wire")
    for name, created in (("owner", 17001.0), ("peer", 17002.0), ("other", 17003.0)):
        comms.register(Thread(name, frozenset(), str(tmp_path), created_at=created))
    comms.register(
        Thread("human", frozenset(), str(tmp_path), role=ThreadRole.USER, created_at=17004.0)
    )
    return comms


def _rows(comms: Comms, name: str):
    snapshot = comms.relationships.snapshot(name)
    group = next(group for group in snapshot.groups if group.key == "collaborating")
    return group.entries, snapshot.unresolved_goal_mentions


def test_exact_goal_mentions_are_mutual_read_only_awareness_with_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms = _wire(tmp_path)
    goal = comms.update_goal(
        "owner", "set", text="Work with @peer and @peer, not @owner or @unknown."
    )
    assert goal is not None
    assert goal.mention_source is not None
    source = goal.mention_source
    assert (source.goal_id, source.text_revision, source.owner_name, source.owner_created_at) == (
        goal.id,
        1,
        "owner",
        17001.0,
    )
    assert [(row.token, row.resolution) for row in source.bindings] == [
        ("peer", "resolved"),
        ("owner", "self"),
        ("unknown", "unknown"),
    ]
    assert not comms.relationships.path.exists()
    owner_rows, diagnostics = _rows(comms, "owner")
    peer_rows, _ = _rows(comms, "peer")
    assert len(owner_rows) == len(peer_rows) == 1
    assert (owner_rows[0].target, peer_rows[0].target) == ("peer", "owner")
    assert owner_rows[0].sources == peer_rows[0].sources == ("goal_mention",)
    contact = owner_rows[0].goal_contacts[0]
    assert asdict(contact) == {
        "owner": "owner",
        "owner_created_at": 17001.0,
        "peer": "peer",
        "peer_created_at": 17002.0,
        "goal_id": goal.id,
        "text_revision": 1,
    }
    assert [(row.token, row.reason) for row in diagnostics] == [
        ("owner", "self"),
        ("unknown", "unknown"),
    ]
    monkeypatch.delenv("PI_AGENT_ID", raising=False)
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    tool = invoke_tool(comms, "comms_collaborations", {})
    assert tool["collaborations"] == []
    assert tool["visible_collaborators"][0]["sources"] == ("goal_mention",)
    assert {item["token"] for item in tool["unresolved_goal_mentions"]} == {"owner", "unknown"}
    reopened = Comms(comms.root)
    assert _rows(reopened, "peer")[0][0].goal_contacts == (contact,)
    assert not comms.relationships.path.exists()
    assert comms.full_history() == []


def test_only_exact_registered_executable_names_bind_without_alias_or_prefix(
    tmp_path: Path,
) -> None:
    comms = _wire(tmp_path)
    comms.registry.rename("other", "renamed")
    goal = comms.update_goal(
        "owner",
        "set",
        text="@peer.bad @peer/path @peer@host @Peer @other @human @owner @renamed",
    )
    assert goal is not None and goal.mention_source is not None
    assert [(row.token, row.resolution) for row in goal.mention_source.bindings] == [
        ("peer.bad", "malformed"),
        ("peer/path", "malformed"),
        ("peer@host", "malformed"),
        ("Peer", "unknown"),
        ("other", "alias"),
        ("human", "non_executable"),
        ("owner", "self"),
        ("renamed", "resolved"),
    ]
    assert [row.target for row in _rows(comms, "owner")[0]] == ["renamed"]
    assert _rows(comms, "peer")[0] == ()


def test_over_limit_goal_mentions_fail_closed_as_a_whole(tmp_path: Path) -> None:
    comms = _wire(tmp_path)
    text = "@peer " + " ".join(f"@unknown{number}" for number in range(129))
    goal = comms.update_goal("owner", "set", text=text)
    assert goal is not None and goal.mention_source is not None
    assert [(row.token, row.resolution) for row in goal.mention_source.bindings] == [
        ("<goal-mentions>", "limit_exceeded")
    ]
    rows, diagnostics = _rows(comms, "owner")
    assert rows == ()
    assert [(row.token, row.reason) for row in diagnostics] == [
        ("<goal-mentions>", "limit_exceeded")
    ]
    assert _rows(comms, "peer")[0] == ()


def test_bound_renames_follow_only_the_same_incarnation_and_name_reuse_is_stale(
    tmp_path: Path,
) -> None:
    comms = _wire(tmp_path)
    goal = comms.update_goal("owner", "set", text="@peer please inspect")
    assert goal is not None
    comms.registry.rename("peer", "reviewer")
    renamed_rows, _ = _rows(comms, "owner")
    assert renamed_rows[0].target == "reviewer"
    assert _rows(comms, "reviewer")[0][0].target == "owner"
    comms.stop("reviewer")
    comms.delete("reviewer")
    comms.register(Thread("peer", frozenset(), str(tmp_path), created_at=18002.0))
    # The old token must not grant a link to a new peer with the same spelling.
    assert _rows(comms, "owner")[0] == ()
    assert _rows(comms, "peer")[0] == ()
    assert [(row.token, row.reason) for row in _rows(comms, "owner")[1]] == [
        ("peer", "stale_incarnation")
    ]
    edited = comms.update_goal("owner", "edit", text="@peer please inspect", goal_id=goal.id)
    assert edited is not None and edited.mention_source is not None
    assert edited.mention_source.text_revision == edited.revision
    assert _rows(comms, "peer")[0][0].goal_contacts[0].peer_created_at == 18002.0


def test_owner_rename_keeps_only_the_bound_owner_incarnation(tmp_path: Path) -> None:
    comms = _wire(tmp_path)
    goal = comms.update_goal("owner", "set", text="@peer")
    assert goal is not None
    comms.registry.rename("owner", "renamed-owner")
    owner_rows, _ = _rows(comms, "renamed-owner")
    peer_rows, _ = _rows(comms, "peer")
    assert owner_rows[0].target == "peer"
    assert peer_rows[0].target == "renamed-owner"
    assert owner_rows[0].goal_contacts[0].owner == "renamed-owner"
    assert goal.mention_source is not None and goal.mention_source.owner_name == "owner"


def test_goal_status_edits_and_explicit_contacts_are_independent(tmp_path: Path) -> None:
    comms = _wire(tmp_path)
    manual = comms.relationships.edit("owner", "add", "peer", "Accepted review separately")
    original = (comms.relationships.path).read_bytes()
    goal = comms.update_goal("owner", "set", text="Please consider @peer")
    assert goal is not None
    rows, _ = _rows(comms, "owner")
    assert len(rows) == 1 and rows[0].sources == ("explicit", "goal_mention")
    assert manual is not None and manual.note in rows[0].detail
    progress = comms.update_goal("owner", "active", goal_id=goal.id, progress="Progress")
    assert progress is not None and progress.revision > goal.revision
    assert _rows(comms, "owner")[0][0].goal_contacts[0].text_revision == 1
    assert comms.relationships.path.read_bytes() == original
    comms.update_goal("owner", "paused", goal_id=goal.id, owner_action=True)
    assert _rows(comms, "owner")[0][0].sources == ("explicit",)
    comms.update_goal("owner", "active", goal_id=goal.id, owner_action=True)
    assert _rows(comms, "owner")[0][0].sources == ("explicit", "goal_mention")
    comms.relationships.edit("owner", "remove", "peer")
    assert _rows(comms, "owner")[0][0].sources == ("goal_mention",)
    comms.relationships.edit("owner", "add", "peer", "Explicit retained note")
    comms.update_goal("owner", "edit", text="No peer mention", goal_id=goal.id)
    assert _rows(comms, "owner")[0][0].sources == ("explicit",)
    assert comms.relationships.collaborations("peer")[0].note == "Explicit retained note"
    comms.update_goal("owner", "edit", text="@peer reconsider", goal_id=goal.id)
    assert _rows(comms, "owner")[0][0].sources == ("explicit", "goal_mention")
    comms.update_goal("owner", "completed", goal_id=goal.id)
    assert _rows(comms, "owner")[0][0].sources == ("explicit",)
    replacement = comms.update_goal("owner", "set", text="@other", goal_id=goal.id)
    assert replacement is not None and replacement.id != goal.id
    assert {row.target for row in _rows(comms, "owner")[0]} == {"other", "peer"}
    assert comms.relationships.collaborations("owner")[0].note == "Explicit retained note"


def test_registry_write_failure_never_exposes_uncommitted_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms = _wire(tmp_path)
    before = comms.registry._path.read_bytes()

    def fail_save() -> None:
        raise OSError("injected registry write failure")

    monkeypatch.setattr(comms.registry, "_save_unlocked", fail_save)
    with pytest.raises(OSError, match="injected registry write failure"):
        comms.update_goal("owner", "set", text="@peer")
    assert comms.registry._path.read_bytes() == before
    fresh = Comms(comms.root)
    assert fresh.registry.require("owner").goal is None
    assert _rows(fresh, "peer")[0] == ()
    assert not fresh.relationships.path.exists()


def test_reused_owner_incarnation_cannot_inherit_old_derived_contact(tmp_path: Path) -> None:
    comms = _wire(tmp_path)
    goal = comms.update_goal("owner", "set", text="@peer")
    assert goal is not None
    comms.stop("owner")
    comms.delete("owner")
    comms.register(Thread("owner", frozenset(), str(tmp_path), goal=goal, created_at=18001.0))
    assert _rows(comms, "peer")[0] == ()
    assert _rows(comms, "owner")[0] == ()
    assert not comms.relationships.path.exists()


def test_old_writer_text_change_drops_derived_links_without_erasing_goal(tmp_path: Path) -> None:
    comms = _wire(tmp_path)
    goal = comms.update_goal("owner", "set", text="@peer")
    assert goal is not None
    path = comms.registry._path
    raw = json.loads(path.read_text())
    raw["threads"]["owner"]["goal"]["text"] = "No mention"
    raw["threads"]["owner"]["goal"]["revision"] += 1
    path.write_text(json.dumps(raw))
    reopened = Comms(comms.root)
    assert reopened.registry.require("owner").goal.text == "No mention"
    assert _rows(reopened, "owner")[0] == ()
    assert reopened.relationships.goal_contacts("owner")[0] == ()
