"""How much one thread matters to another, and what its peers see of that."""

import json

import pytest

from agent_comms.comms import Comms
from agent_comms.goal_actions import SetGoalAction
from agent_comms.peer_relevance import (
    AlwaysListed,
    ChannelmateRelevance,
    CollaboratorRelevance,
    GoalContactRelevance,
    PeerRelevance,
    RelevanceCapability,
    StrangerRelevance,
    Visible,
)
from agent_comms.relationships import RelationshipEdit
from agent_comms.threads import Thread
from agent_comms.turn_context import CoordinationSegment, NextContextTurn, TurnContext


def _wire(tmp_path, *threads):
    comms = Comms(tmp_path / "wire")
    for created, (name, tags) in enumerate(threads, start=1):
        comms.registry.declare(
            Thread(name, frozenset(tags), str(tmp_path), created_at=20000.0 + created)
        )
    return comms


def _peers(comms, name):
    owner = comms.registry.require(name)
    context = TurnContext.for_owner(
        owner, NextContextTurn(), "", comms.views.thread_views(),
        comms.relationships.peer_relations(name),
    )
    return next(s for s in context.segments if isinstance(s, CoordinationSegment)).peers


def test_each_member_guarantees_what_the_member_it_derives_from_guarantees():
    members = PeerRelevance.members_with(PeerRelevance)
    capabilities = RelevanceCapability.__subclasses__()
    for member in members:
        assert PeerRelevance.most_derived((StrangerRelevance, member)) is member
        for base in members:
            if issubclass(member, base):
                assert all(member.grants(c) for c in capabilities if base.grants(c))
    assert PeerRelevance.most_derived(members) is CollaboratorRelevance
    assert not any(StrangerRelevance.grants(capability) for capability in capabilities)
    with pytest.raises(TypeError):
        PeerRelevance.most_derived(())


def test_relevance_is_the_most_derived_member_the_relations_grant(tmp_path):
    comms = _wire(
        tmp_path, ("owner", {"team"}), ("mate", {"team"}), ("partner", ()), ("mentioned", ()),
        ("nobody", {"elsewhere"}),
    )
    comms.relationships.edit("partner", RelationshipEdit.decode("add"), "owner", "Shared review")
    comms.goals.update_goal("owner", SetGoalAction(text="Ship it with @mentioned and @partner."))
    relevance = comms.relationships.relevance
    assert relevance("owner", "partner") is relevance("partner", "owner") is CollaboratorRelevance
    assert relevance("owner", "mentioned") is relevance("mentioned", "owner") is GoalContactRelevance
    assert relevance("owner", "mate") is ChannelmateRelevance
    assert relevance("owner", "nobody") is relevance("mate", "partner") is StrangerRelevance
    with pytest.raises(ValueError):
        relevance("owner", "owner")


def test_a_thread_reusing_a_deleted_name_inherits_no_relevance(tmp_path):
    comms = _wire(tmp_path, ("owner", ()), ("partner", ()))
    comms.relationships.edit("owner", RelationshipEdit.decode("add"), "partner", "Old work")
    comms.owners.stop("partner")
    comms.registry.remove("partner")
    comms.registry.declare(Thread("partner", frozenset(), str(tmp_path), created_at=30000.0))
    assert comms.relationships.relevance("owner", "partner") is StrangerRelevance


def test_peer_list_keeps_every_related_peer_whatever_its_name_or_the_thread_count(tmp_path):
    slots = CoordinationSegment.PEER_SLOTS
    comms = _wire(
        tmp_path, ("owner", {"team"}), ("zz-partner", ()), ("zy-mentioned", ()),
        ("aa-stranger", ()), *((f"mate-{index:03}", {"team"}) for index in range(slots + 10)),
    )
    comms.relationships.edit("owner", RelationshipEdit.decode("add"), "zz-partner", "Shared review")
    comms.goals.update_goal("zz-partner", SetGoalAction(text="Review the parser."))
    comms.goals.update_goal("owner", SetGoalAction(text="Ship it with @zy-mentioned."))
    peers = _peers(comms, "owner")
    assert [peer.name for peer in peers[:3]] == ["zz-partner", "zy-mentioned", "mate-000"]
    assert len(peers) == slots and "aa-stranger" not in {peer.name for peer in peers}
    partner, mentioned, mate = peers[:3]
    assert (partner.relevance, partner.note, partner.goal) == (
        CollaboratorRelevance, "Shared review", "Review the parser.")
    assert mentioned.relevance is GoalContactRelevance and "owner's goal" in mentioned.note
    assert (mate.relevance, mate.note, mate.goal) == (ChannelmateRelevance, "", None)
    assert all(peer.relevance.grants(Visible) for peer in peers)
    assert [peer.relevance.grants(AlwaysListed) for peer in peers[:3]] == [True, True, False]
    shown = json.loads(_coordination_text(comms, "owner").split("most relevant first: ")[1]
                       .split("\n\n")[0])
    assert shown[0]["relevance"] == "collaborator" and shown[0]["goal"] == "Review the parser."


def _coordination_text(comms, name):
    owner = comms.registry.require(name)
    return CoordinationSegment.capture(
        owner, comms.views.thread_views(), comms.relationships.peer_relations(name)
    ).text()
