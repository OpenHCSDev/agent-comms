"""One original decision/correction journey through declared tools and wire custody."""

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import Goal
from agent_comms.messages import Message, MessageType
from agent_comms.task_decisions import Decision, DecisionScope
from agent_comms.thread_identity import TurnId
from agent_comms.threads import Thread
from agent_comms.tools import invoke_tool, tool_catalog


def admit(comms, name):
    comms.registry.declare(Thread(
        name, frozenset({"team"}), str(comms.root),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    owner, admission = comms.registry.live_owner_with_admission(name)
    owner, _ = comms.registry.lease_live_turn_with_admission(
        owner, name + "-turn", expected_generation=admission
    )
    return owner


def test_original_choice_correction_and_authority_survive_reopen(comms, monkeypatch):
    alpha = admit(comms, "alpha")
    beta = admit(comms, "beta")
    monkeypatch.setenv("PI_AGENT_ID", "alpha")
    request = {"chosen": "Keep /artifacts/exact-root", "rejected": ["Use /scratch/guess-root"], "to": "#team"}
    first = invoke_tool(comms, "comms_decision", request)
    original = comms.bus.log.full_history()[0]
    assert original.reference == FieldCodec.decode(type(original.reference), first["reference"])
    assert original.decision.author == alpha.incarnation
    assert original.decision.source_turn == alpha.turn_identity
    assert original.notice and not original.starts_turn
    assert Message.from_committed_wire(original.to_wire()) == original
    correction = invoke_tool(comms, "comms_decision", {
        **request, "chosen": "Keep /artifacts/certified-root", "supersedes": first["reference"],
    })
    rows = comms.bus.log.full_history()
    assert len(rows) == 2 and rows[0] == original
    assert rows[1].decision.supersedes == original.reference
    assert correction["reference"] == FieldCodec.encode(rows[1].reference)
    from agent_comms.retained_task_facts import RetainedTaskFacts

    with comms.bus.log.locked():
        revision, facts = comms.bus.log.compaction_messages_unlocked(
            alpha.incarnation
        )
    retained = RetainedTaskFacts(facts)
    assert tuple(fact.source.reference for fact in retained.facts) == tuple(
        row.reference for row in rows
    )
    assert FieldCodec.decode(RetainedTaskFacts, FieldCodec.encode(retained)) == retained
    assert original.decision.chosen in retained.text
    assert original.decision.rejected[0] in retained.text
    assert rows[1].decision.supersedes.message_id in retained.text
    with pytest.raises(RelationViolationError, match="omitted"):
        retained.require_summary("Assistant prose cannot replace original choices")
    retained.require_summary(retained.text + "\n\nNarrative")
    bus = comms.root / "bus.jsonl"
    before = bus.read_bytes()
    monkeypatch.setenv("PI_AGENT_ID", "beta")
    with pytest.raises(RelationViolationError, match="another author's"):
        invoke_tool(comms, "comms_decision", {**request, "supersedes": first["reference"]})
    assert bus.read_bytes() == before
    assert comms.registry.require("beta").turn_lease == beta.turn_lease
    monkeypatch.setenv("PI_AGENT_ID", "alpha")
    with pytest.raises(ValueError, match="Unknown fields"):
        invoke_tool(comms, "comms_decision", {**request, "author": "beta"})
    comms.registry.release_turn(alpha.turn_lease)
    with pytest.raises(ValueError, match="admitted"):
        invoke_tool(comms, "comms_decision", request)
    assert bus.read_bytes() == before
    from agent_comms.comms import Comms

    assert Comms(comms.root).bus.log.full_history() == rows
    schema = next(tool for tool in tool_catalog() if tool["name"] == "comms_decision")
    assert set(schema["parameters"]["required"]) == {"chosen", "rejected", "to"}
    assert "author" not in schema["parameters"]["properties"]


def test_stale_goal_scope_cannot_publish_even_through_original_publisher(comms):
    owner = admit(comms, "alpha")
    owner = replace(owner, goal=Goal("Keep exact scope", "goal", revision=1))
    comms.registry.register(owner)
    declaration = Decision(
        chosen="A", rejected=("B",), scope=DecisionScope.for_owner(owner),
        source_turn=owner.turn_identity,
        source_turn_id=TurnId(owner.active_turn.id),
    )
    comms.registry.register(replace(owner, goal=replace(owner.goal, revision=2)))
    message = Message("alpha", "#team", declaration.text, MessageType.INFO, decision=declaration)
    with pytest.raises(ValueError, match="goal changed"):
        comms.bus.publisher.publish_ordinary(message)
    assert not comms.bus.log.full_history()


@pytest.mark.parametrize("chosen,rejected", [("", ("B",)), ("A", ()), ("A", ("A",)), ("A", ("B", "B"))])
def test_invalid_alternatives_do_not_publish(comms, monkeypatch, chosen, rejected):
    admit(comms, "alpha")
    monkeypatch.setenv("PI_AGENT_ID", "alpha")
    with pytest.raises(ValueError):
        invoke_tool(comms, "comms_decision", {"chosen": chosen, "rejected": list(rejected), "to": "#team"})
    assert not comms.bus.log.full_history()
