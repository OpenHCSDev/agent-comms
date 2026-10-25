"""How much one thread matters to another, and what its peers see of that."""

from agent_comms.repo import Checkout
from conftest import publish_initial
import json

import pytest

from conftest import installed_comms
from agent_comms.context_segments.partner_activity import PartnerActivitySegment
from agent_comms.goal_actions import CompletedGoalAction, GoalPrecondition, SetGoalAction
from agent_comms.goal_states import ActiveGoal, CompletedGoal
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
from agent_comms.thread_text import ForkedParent
from agent_comms.threads import Thread
from agent_comms.turn_context import CoordinationSegment, NextContextTurn, TurnContext
from agent_comms.goal_scope import OneGoal


def _wire(tmp_path, *threads):
    comms = installed_comms(tmp_path / "wire")
    for created, (name, tags) in enumerate(threads, start=1):
        comms.registry.declare(
            Thread(name, frozenset(tags), Checkout.from_wire(str(tmp_path)), created_at=20000.0 + created)
        )
    return comms


def _peers(comms, name):
    owner = comms.registry.require(name)
    context = TurnContext.for_owner(
        owner, NextContextTurn(), "", comms.views.thread_views(),
        comms.relationships.context_relations(name), comms.goals,
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
    comms.relationships.edit("partner", RelationshipEdit.member_named("add"), "owner", "Shared review")
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
    comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "partner", "Old work")
    comms.owners.stop("partner")
    comms.registry.remove("partner")
    comms.registry.declare(Thread('partner', frozenset(), Checkout.from_wire(str(tmp_path)), created_at=30000.0))
    assert comms.relationships.relevance("owner", "partner") is StrangerRelevance


def test_peer_list_keeps_every_related_peer_whatever_its_name_or_the_thread_count(tmp_path):
    comms = _wire(
        tmp_path, ("owner", {"team"}), ("zz-partner", ()), ("zy-mentioned", ()),
        ("aa-stranger", ()), *((f"mate-{index:03}", {"team"}) for index in range(20)),
    )
    comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "zz-partner", "Shared review")
    comms.goals.update_goal("zz-partner", SetGoalAction(text="Review the parser."))
    comms.goals.update_goal("owner", SetGoalAction(text="Ship it with @zy-mentioned."))
    comms.goals.update_goal("mate-000", SetGoalAction(text="Unrelated work."))
    peers = _peers(comms, "owner")
    assert [peer.name for peer in peers] == ["zz-partner", "zy-mentioned"]
    assert not {"aa-stranger", "mate-000"} & {peer.name for peer in peers}
    partner, mentioned = peers
    assert (partner.relevance, partner.note, partner.goal) == (
        CollaboratorRelevance, "Shared review", ("Review the parser.",))
    assert mentioned.relevance is GoalContactRelevance and "owner's goal" in mentioned.note
    assert all(peer.relevance.grants(Visible) for peer in peers)
    assert all(peer.relevance.grants(AlwaysListed) for peer in peers)
    shown = json.loads(_coordination_text(comms, "owner").split("Peer state: ")[1].splitlines()[0])
    assert shown[0]["relevance"] == "collaborator" and shown[0]["goal"] == ["Review the parser."]


def _coordination_text(comms, name):
    owner = comms.registry.require(name)
    return CoordinationSegment.capture(
        owner, comms.views.thread_views(), comms.relationships.context_relations(name), comms.goals
    ).text()


def _partner_activity(comms, name):
    context = TurnContext.for_inspection(comms, comms.registry.require(name))
    return tuple(s for s in context.segments if isinstance(s, PartnerActivitySegment))


def test_stopped_channelmates_do_not_fill_peer_slots_but_contacts_remain(tmp_path):
    comms = _wire(
        tmp_path, ('owner', {'team'}), ('partner', ()), ('contact', ()),
        ('live-mate', {'team'}), ('stopped-mate', {'team'}),
    )
    comms.relationships.edit('owner', RelationshipEdit.member_named('add'), 'partner', 'Shared review')
    comms.goals.update_goal('owner', SetGoalAction(text='Work with @contact.'))
    for name in ('partner', 'contact', 'stopped-mate'):
        comms.owners.stop(name)
    peers = _peers(comms, 'owner')
    assert [peer.name for peer in peers] == ['partner', 'contact']


def test_a_collaborators_goal_change_reaches_its_partner_as_a_typed_item_without_a_wake(tmp_path):
    comms = _wire(tmp_path, ("owner", {"team"}), ("partner", ()), ("mate", {"team"}))
    comms.relationships.edit("owner", RelationshipEdit.member_named("add"), "partner", "Shared review")
    assert _partner_activity(comms, "owner") == ()
    comms.goals.update_goal("mate", SetGoalAction(text="Unrelated work."))
    goal = comms.goals.update_goal("partner", SetGoalAction(text="Review the parser."))
    (segment,) = _partner_activity(comms, "owner")
    (change,) = segment.changes
    assert (change.peer, change.goal, change.state) == ("partner", goal.revision_key, ActiveGoal)
    comms.goals.update_goal(
        "partner", CompletedGoalAction(expect=GoalPrecondition(goal_id=OneGoal(goal.id)), progress="Done.")
    )
    (settled,) = _partner_activity(comms, "owner")[0].changes
    assert (settled.state, settled.progress) == (CompletedGoal, "Done.")
    shown = json.loads(_partner_activity(comms, "owner")[0].text().splitlines()[-1])
    assert shown[0]["peer"] == "partner" and shown[0]["state"] == "completed"
    # The change is read at the partner's next turn: nothing was sent and nobody was woken.
    assert comms.bus.bus.revision() == 0
    assert comms.bus.pending_counts_all(["owner", "partner", "mate"]) == dict.fromkeys(
        ("owner", "partner", "mate"), 0
    )
    assert _partner_activity(comms, "mate") == ()


def test_awareness_shows_a_related_peers_messages_first_within_the_same_window(tmp_path):
    from bus_record_fixture import context_comms

    comms = context_comms(tmp_path)
    earlier = publish_initial(comms, "outsider", "Alice", "From a stranger")
    later = publish_initial(comms, "Bob", "Alice", "From a partner")
    owner = comms.registry.require("Alice")

    def shown():
        relations = comms.relationships.peer_relations("Alice")
        (segment,) = comms.bus.awareness_segments(owner, relations)
        return [row.seq for row in segment.sources]

    assert shown() == [earlier.seq, later.seq]
    comms.relationships.edit("Alice", RelationshipEdit.member_named("add"), "Bob", "Shared review")
    assert shown() == [later.seq, earlier.seq]


def test_context_shows_each_relation_with_its_author_time_note_and_history(tmp_path):
    from agent_comms.context_segments.relationships import RelationshipsSegment

    comms = _wire(tmp_path, ("owner", ()), ("partner", ()), ("mentioned", ()), ("nobody", ()))

    def records(name):
        context = TurnContext.for_inspection(comms, comms.registry.require(name))
        segments = [s for s in context.segments if isinstance(s, RelationshipsSegment)]
        return segments[0].records if segments else ()

    assert records("owner") == ()
    created = comms.relationships.edit(
        "partner", RelationshipEdit.member_named("add"), "owner", "First plan")
    updated = comms.relationships.edit(
        "owner", RelationshipEdit.member_named("update"), "partner", "Second plan")
    comms.goals.update_goal("owner", SetGoalAction(text="Ship it with @mentioned."))
    collaboration, mention = records("owner")
    assert (collaboration.peer, collaboration.relevance, collaboration.author) == (
        "partner", CollaboratorRelevance, "partner")
    assert (collaboration.created_at, collaboration.note) == (created.created_at, "Second plan")
    assert [(revision.note, revision.at) for revision in collaboration.history] == [
        ("First plan", created.updated_at)]
    assert updated.updated_at > created.updated_at
    assert (mention.peer, mention.relevance, mention.author, mention.created_at) == (
        "mentioned", GoalContactRelevance, "owner", None)
    assert records("partner")[0].peer == "owner" and records("partner")[0].author == "partner"
    assert records("nobody") == ()
    context = TurnContext.for_inspection(comms, comms.registry.require("owner"))
    shown = json.loads(next(
        s for s in context.segments if isinstance(s, RelationshipsSegment)
    ).text().splitlines()[-1])
    assert shown[0]["history"] == [{"note": "First plan", "at": created.updated_at}]


def test_recent_direct_work_stays_addressable_without_bare_peer_inventory(tmp_path):
    from bus_record_fixture import context_comms

    comms = context_comms(tmp_path)
    original = publish_initial(comms, "Bob", "Alice", "Current work")
    assert [r.peer.name for r in comms.relationships.context_relations("Alice")] == ["Bob"]
    assert _peers(comms, "Alice") == ()
    comms.owners.stop("Bob")
    assert [r.peer.name for r in comms.relationships.context_relations("Alice")] == ["Bob"]
    comms.registry.remove("Bob")
    comms.registry.declare(Thread('Bob', frozenset(), Checkout.from_wire(str(tmp_path)), created_at=original.timestamp + 1))
    assert _peers(comms, "Alice") == ()
    assert comms.relationships.context_relations("Alice") == ()


def test_parent_binding_remains_without_bare_parent_inventory(tmp_path):
    comms = _wire(tmp_path, ("parent", ()), ("unrelated", ()))
    comms.registry.declare(Thread('child', frozenset(), Checkout.from_wire(str(tmp_path)), parent=ForkedParent('parent')))
    context = TurnContext.for_inspection(comms, comms.registry.require("child"))
    segment = next(s for s in context.segments if isinstance(s, CoordinationSegment))
    assert (segment.parent, segment.peers) == ("parent", ())
