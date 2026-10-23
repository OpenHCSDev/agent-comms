"""Goal mentions are incarnation-bound contact awareness, not accepted work."""

from __future__ import annotations


import json
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_comms.relationships import CollaboratingRelationshipGroup, RelationshipEdit
from agent_comms.comms import Comms, wire
from conftest import installed_comms
from agent_comms.goal_actions import (
    ActiveGoalAction,
    CompletedGoalAction,
    EditGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
)
from agent_comms.goals import (
    AliasMentionBinding, LimitExceededMentionBinding, MalformedMentionBinding, NonExecutableMentionBinding, ResolvedMentionBinding, SelfMentionBinding, UnknownMentionBinding,
)
from agent_comms.thread_identity import UserThreadRole
from agent_comms.tools import invoke_tool
from agent_comms.tool_caller import ThreadCaller


def _wire(tmp_path: Path) -> Comms:
    comms = installed_comms(tmp_path / "wire")
    for name, created in (("owner", 17001.0), ("peer", 17002.0), ("other", 17003.0)):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path), created_at=created))
    comms.registry.declare(
        comms.registry.new_thread('human', frozenset(), str(tmp_path), role=UserThreadRole, created_at=17004.0)
    )
    return comms


def _rows(comms: Comms, name: str):
    snapshot = comms.relationships.snapshot(name)
    group = snapshot.groups_by_type[CollaboratingRelationshipGroup]
    return group.entries, snapshot.unresolved_goal_mentions


def test_exact_goal_mentions_are_mutual_read_only_awareness_with_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms = _wire(tmp_path)
    goal = comms.goals.update_goal(
        "owner", SetGoalAction(text="Work with @peer and @peer, not @owner or @unknown.")
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
    assert [(row.token, type(row)) for row in source.bindings] == [
        ("peer", ResolvedMentionBinding),
        ("owner", SelfMentionBinding),
        ("unknown", UnknownMentionBinding),
    ]
    assert not comms.relationships.store.path.exists()
    owner_rows, diagnostics = _rows(comms, "owner")
    peer_rows, _ = _rows(comms, "peer")
    assert len(owner_rows) == len(peer_rows) == 1
    assert (owner_rows["peer"].target, peer_rows["owner"].target) == ("peer", "owner")
    assert owner_rows["peer"].sources == peer_rows["owner"].sources == ("goal_mention",)
    contact = owner_rows["peer"].goal_contacts[0]
    assert asdict(contact) == {
        "owner": "owner",
        "owner_created_at": 17001.0,
        "peer": "peer",
        "peer_created_at": 17002.0,
        "goal_id": goal.id,
        "text_revision": 1,
    }
    assert [(row.token, row.reason) for row in diagnostics] == [
        ("owner", SelfMentionBinding),
        ("unknown", UnknownMentionBinding),
    ]
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    tool = invoke_tool(comms, "comms_collaborations", {}, caller=ThreadCaller("owner"))
    assert tool["collaborations"] == []
    assert tool["visible_collaborators"][0]["sources"] == ["goal_mention"]
    assert {item["token"] for item in tool["unresolved_goal_mentions"]} == {"owner", "unknown"}
    reopened = wire(comms.root)
    assert _rows(reopened, "peer")[0]["owner"].goal_contacts == (contact,)
    assert not comms.relationships.store.path.exists()
    assert comms.bus.bus.revision() == 0


def test_only_exact_registered_executable_names_bind_without_alias_or_prefix(
    tmp_path: Path,
) -> None:
    comms = _wire(tmp_path)
    comms.registry.rename("other", "renamed")
    goal = comms.goals.update_goal(
        "owner",
        SetGoalAction(text="@peer.bad @peer/path @peer@host @Peer @other @human @owner @renamed"),
    )
    assert goal is not None and goal.mention_source is not None
    assert [(row.token, type(row)) for row in goal.mention_source.bindings] == [
        ("peer.bad", MalformedMentionBinding),
        ("peer/path", MalformedMentionBinding),
        ("peer@host", MalformedMentionBinding),
        ("Peer", UnknownMentionBinding),
        ("other", AliasMentionBinding),
        ("human", NonExecutableMentionBinding),
        ("owner", SelfMentionBinding),
        ("renamed", ResolvedMentionBinding),
    ]
    assert [row.target for row in _rows(comms, "owner")[0].values()] == ["renamed"]
    assert _rows(comms, "peer")[0] == {}


def test_over_limit_goal_mentions_fail_closed_as_a_whole(tmp_path: Path) -> None:
    comms = _wire(tmp_path)
    text = "@peer " + " ".join(f"@unknown{number}" for number in range(129))
    goal = comms.goals.update_goal("owner", SetGoalAction(text=text))
    assert goal is not None and goal.mention_source is not None
    assert [(row.token, type(row)) for row in goal.mention_source.bindings] == [
        ("<goal-mentions>", LimitExceededMentionBinding)
    ]
    rows, diagnostics = _rows(comms, "owner")
    assert rows == {}
    assert [(row.token, row.reason) for row in diagnostics] == [
        ("<goal-mentions>", LimitExceededMentionBinding)
    ]
    assert _rows(comms, "peer")[0] == {}


def test_bound_renames_follow_only_the_same_incarnation_and_name_reuse_is_stale(
    tmp_path: Path,
) -> None:
    comms = _wire(tmp_path)
    goal = comms.goals.update_goal("owner", SetGoalAction(text="@peer please inspect"))
    assert goal is not None
    comms.registry.rename("peer", "reviewer")
    renamed_rows, _ = _rows(comms, "owner")
    assert renamed_rows["reviewer"].target == "reviewer"
    assert _rows(comms, "reviewer")[0]["owner"].target == "owner"
    comms.owners.stop("reviewer")
    comms.registry.remove("reviewer")
    comms.registry.declare(comms.registry.new_thread('peer', frozenset(), str(tmp_path), created_at=18002.0))
    # The old token must not grant a link to a new peer with the same spelling.
    assert _rows(comms, "owner")[0] == {}
    assert _rows(comms, "peer")[0] == {}
    assert [(row.token, row.reason) for row in _rows(comms, "owner")[1]] == [
        ("peer", "stale_incarnation")
    ]
    edited = comms.goals.update_goal(
        "owner",
        EditGoalAction(expect=GoalPrecondition(goal_id=goal.id), text="@peer please inspect"),
    )
    assert edited is not None and edited.mention_source is not None
    assert edited.mention_source.text_revision == edited.revision
    assert _rows(comms, "peer")[0]["owner"].goal_contacts[0].peer_created_at == 18002.0


def test_owner_rename_keeps_only_the_bound_owner_incarnation(tmp_path: Path) -> None:
    comms = _wire(tmp_path)
    goal = comms.goals.update_goal("owner", SetGoalAction(text="@peer"))
    assert goal is not None
    comms.registry.rename("owner", "renamed-owner")
    owner_rows, _ = _rows(comms, "renamed-owner")
    peer_rows, _ = _rows(comms, "peer")
    assert owner_rows["peer"].target == "peer"
    assert peer_rows["renamed-owner"].target == "renamed-owner"
    assert owner_rows["peer"].goal_contacts[0].owner == "renamed-owner"
    assert goal.mention_source is not None and goal.mention_source.owner_name == "owner"


def test_goal_status_edits_and_explicit_contacts_are_independent(tmp_path: Path) -> None:
    comms = _wire(tmp_path)
    manual = comms.relationships.edit(
        "owner", RelationshipEdit.member_named("add"), "peer", "Accepted review separately"
    )
    original = (comms.relationships.store.path).read_bytes()
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Please consider @peer"))
    assert goal is not None
    rows, _ = _rows(comms, "owner")
    assert len(rows) == 1 and rows["peer"].sources == ("explicit", "goal_mention")
    assert manual is not None and manual.note in rows["peer"].detail
    progress = comms.goals.update_goal(
        "owner", ActiveGoalAction(expect=GoalPrecondition(goal_id=goal.id), progress="Progress")
    )
    assert progress is not None and progress.revision > goal.revision
    assert _rows(comms, "owner")[0]["peer"].goal_contacts[0].text_revision == 1
    assert comms.relationships.store.path.read_bytes() == original
    comms.goals.update_goal(
        "owner", PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)), actor=OwnerInvocable
    )
    assert _rows(comms, "owner")[0]["peer"].sources == ("explicit",)
    comms.goals.update_goal(
        "owner", ActiveGoalAction(expect=GoalPrecondition(goal_id=goal.id)), actor=OwnerInvocable
    )
    assert _rows(comms, "owner")[0]["peer"].sources == ("explicit", "goal_mention")
    comms.relationships.edit("owner", RelationshipEdit.member_named("remove"), "peer")
    assert _rows(comms, "owner")[0]["peer"].sources == ("goal_mention",)
    comms.relationships.edit(
        "owner", RelationshipEdit.member_named("add"), "peer", "Explicit retained note"
    )
    comms.goals.update_goal(
        "owner", EditGoalAction(expect=GoalPrecondition(goal_id=goal.id), text="No peer mention")
    )
    assert _rows(comms, "owner")[0]["peer"].sources == ("explicit",)
    assert comms.relationships.collaborations("peer")[0].note == "Explicit retained note"
    comms.goals.update_goal(
        "owner", EditGoalAction(expect=GoalPrecondition(goal_id=goal.id), text="@peer reconsider")
    )
    assert _rows(comms, "owner")[0]["peer"].sources == ("explicit", "goal_mention")
    comms.goals.update_goal("owner", CompletedGoalAction(expect=GoalPrecondition(goal_id=goal.id)))
    assert _rows(comms, "owner")[0]["peer"].sources == ("explicit",)
    replacement = comms.goals.update_goal(
        "owner", SetGoalAction(expect=GoalPrecondition(goal_id=goal.id), text="@other")
    )
    assert replacement is not None and replacement.id != goal.id
    assert {row.target for row in _rows(comms, "owner")[0].values()} == {"other", "peer"}
    assert comms.relationships.collaborations("owner")[0].note == "Explicit retained note"


def test_reused_owner_incarnation_cannot_inherit_old_derived_contact(tmp_path: Path) -> None:
    comms = _wire(tmp_path)
    goal = comms.goals.update_goal("owner", SetGoalAction(text="@peer"))
    assert goal is not None
    comms.owners.stop("owner")
    comms.registry.remove("owner")
    comms.registry.declare(
        comms.registry.new_thread('owner', frozenset(), str(tmp_path), goal=goal, created_at=18001.0)
    )
    assert _rows(comms, "peer")[0] == {}
    assert _rows(comms, "owner")[0] == {}
    assert not comms.relationships.store.path.exists()


def test_old_writer_text_change_drops_derived_links_without_erasing_goal(tmp_path: Path) -> None:
    comms = _wire(tmp_path)
    goal = comms.goals.update_goal("owner", SetGoalAction(text="@peer"))
    assert goal is not None
    path = comms.registry.store.path
    raw = json.loads(path.read_text())
    raw["threads"]["owner"]["goal"]["text"] = "No mention"
    raw["threads"]["owner"]["goal"]["revision"] += 1
    path.write_text(json.dumps(raw))
    reopened = wire(comms.root)
    assert reopened.registry.require("owner").goal.text == "No mention"
    assert _rows(reopened, "owner")[0] == {}
    assert reopened.relationships.goal_contacts("owner")[0] == ()
