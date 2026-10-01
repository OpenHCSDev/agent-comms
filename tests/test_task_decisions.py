"""One original decision/correction journey through declared tools and wire custody."""

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import Goal
from agent_comms.messages import Message, MessageType
from agent_comms.task_decisions import Decision
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
        **request, "chosen": "Keep /artifacts/certified-root", "change": {"kind": "correction", "original": first["reference"]},
    })
    rows = comms.bus.log.full_history()
    assert len(rows) == 2 and rows[0] == original
    assert rows[1].decision.change.original == original.reference
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
    assert rows[1].decision.change.original.message_id in retained.text
    assert retained.current_decisions(alpha, comms.registry.snapshot()) == (rows[1],)
    with pytest.raises(RelationViolationError, match="omitted"):
        retained.require_summary("Assistant prose cannot replace original choices")
    retained.require_summary(retained.text + "\n\nNarrative")
    bus = comms.root / "bus.jsonl"
    before = bus.read_bytes()
    monkeypatch.setenv("PI_AGENT_ID", "beta")
    with pytest.raises(RelationViolationError, match="another author's"):
        invoke_tool(comms, "comms_decision", {**request, "change": {"kind": "correction", "original": first["reference"]}})
    assert bus.read_bytes() == before
    assert comms.registry.require("beta").turn_lease == beta.turn_lease
    monkeypatch.setenv("PI_AGENT_ID", "alpha")
    with pytest.raises(ValueError, match="Unknown fields"):
        invoke_tool(comms, "comms_decision", {**request, "author": "beta"})
    comms.registry.rename("alpha", "renamed-alpha")
    renamed = comms.registry.require("renamed-alpha")
    assert retained.current_decisions(renamed, comms.registry.snapshot()) == (rows[1],)
    monkeypatch.setenv("PI_AGENT_ID", "renamed-alpha")
    comms.registry.release_turn(alpha.turn_lease)
    assert retained.current_decisions(comms.registry.require("alpha"), comms.registry.snapshot()) == ()
    with pytest.raises(ValueError, match="admitted"):
        invoke_tool(comms, "comms_decision", request)
    assert bus.read_bytes() == before
    from agent_comms.comms import Comms

    assert Comms(comms.root).bus.log.full_history() == rows
    schema = next(tool for tool in tool_catalog() if tool["name"] == "comms_decision")
    assert set(schema["parameters"]["required"]) == {"chosen", "rejected", "to"}
    assert "author" not in schema["parameters"]["properties"]


def test_equal_text_choices_keep_original_identity_through_rename_and_goal_replacement(comms, monkeypatch):
    from agent_comms.retained_task_facts import CurrentDecisionTaskFact, RetainedTaskFacts

    owner = admit(comms, "alpha")
    owner = replace(owner, goal=Goal("Keep certified root", "goal", revision=1))
    comms.registry.register(owner)
    monkeypatch.setenv("PI_AGENT_ID", "alpha")
    request = {"chosen": "/artifacts/exact", "rejected": ["/scratch/guess"], "to": "#team"}
    first = invoke_tool(comms, "comms_decision", request)
    invoke_tool(comms, "comms_decision", request)
    originals = comms.bus.log.full_history()
    assert originals[0].body == originals[1].body
    assert originals[0].reference != originals[1].reference
    comms.registry.rename("alpha", "renamed-alpha")
    renamed = comms.registry.require("renamed-alpha")
    monkeypatch.setenv("PI_AGENT_ID", "renamed-alpha")
    invoke_tool(comms, "comms_decision", {
        **request, "chosen": "/artifacts/certified",
        "change": {"kind": "correction", "original": first["reference"]},
    })
    snapshot = comms.registry.snapshot()
    with comms.bus.log.locked():
        _, facts = comms.bus.log.compaction_messages_unlocked(
            renamed.incarnation)
    retained = RetainedTaskFacts(facts).for_owner(renamed, snapshot)
    rows = comms.bus.log.full_history()
    assert tuple(fact.source.reference for fact in retained.facts) == tuple(row.reference for row in rows)
    assert retained.current_decisions(renamed, snapshot) == (rows[2], rows[1])
    assert tuple(fact.source.reference for fact in retained.facts
                 if isinstance(fact, CurrentDecisionTaskFact)) == (rows[1].reference, rows[2].reference)
    assert FieldCodec.decode(RetainedTaskFacts, FieldCodec.encode(retained)) == retained
    before = (comms.root / "bus.jsonl").read_bytes()
    replacement = replace(renamed, goal=Goal("New task", "replacement", revision=1))
    comms.registry.register(replacement)
    stale = retained.for_owner(replacement, comms.registry.snapshot())
    assert not any(isinstance(fact, CurrentDecisionTaskFact) for fact in stale.facts)
    assert tuple(fact.source for fact in stale.facts) == tuple(fact.source for fact in retained.facts)
    comms.registry.remove(renamed.name)
    assert retained.current_decisions(renamed, comms.registry.snapshot()) == ()
    assert (comms.root / "bus.jsonl").read_bytes() == before


def test_stale_goal_scope_cannot_publish_even_through_original_publisher(comms):
    owner = admit(comms, "alpha")
    owner = replace(owner, goal=Goal("Keep exact scope", "goal", revision=1))
    comms.registry.register(owner)
    declaration = Decision(
        chosen="A", rejected=("B",), scope=owner.decision_scope,
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


def test_turn_scope_correction_cannot_revive_a_finished_turn_choice(comms, monkeypatch):
    owner = admit(comms, "alpha")
    monkeypatch.setenv("PI_AGENT_ID", "alpha")
    request = {"chosen": "A", "rejected": ["B"], "to": "#team"}
    first = invoke_tool(comms, "comms_decision", request)
    comms.registry.release_turn(owner.turn_lease)
    owner, admission = comms.registry.live_owner_with_admission("alpha")
    comms.registry.lease_live_turn_with_admission(owner, "next-turn", expected_generation=admission)
    before = (comms.root / "bus.jsonl").read_bytes()
    with pytest.raises(RelationViolationError, match="different turn scope"):
        invoke_tool(comms, "comms_decision", {
            **request, "chosen": "C",
            "change": {"kind": "correction", "original": first["reference"]},
        })
    assert (comms.root / "bus.jsonl").read_bytes() == before


def test_exact_user_supersession_cannot_be_impersonated_or_overridden_by_peer_choice(comms, monkeypatch):
    from agent_comms.retained_task_facts import CurrentUserDecisionCorrectionTaskFact, RetainedTaskFacts
    from agent_comms.task_decisions import CorrectionDecisionChange, UserDecisionSupersession

    owner = admit(comms, "alpha")
    owner = replace(owner, goal=Goal("Keep project scope", "goal", revision=1))
    comms.registry.register(owner)
    monkeypatch.setenv("PI_AGENT_ID", "alpha")
    request = {"chosen": "/artifacts/old", "rejected": ["/scratch/guess"], "to": "#team"}
    first = invoke_tool(comms, "comms_decision", request)
    original = comms.bus.log.full_history()[0]
    declaration = UserDecisionSupersession(CorrectionDecisionChange(original.reference))
    before = (comms.root / "bus.jsonl").read_bytes()
    with pytest.raises(RelationViolationError, match="human sender"):
        comms.bus.publisher.publish_ordinary(
            Message(owner.name, "#team", "A peer is not the user", MessageType.INFO, decision=declaration))
    assert (comms.root / "bus.jsonl").read_bytes() == before
    corrected = comms.messaging.send_user_message(
        "#team", "Retract the old path choice. Never replay UNKNOWN.",
        worktree=owner.worktree, decision=declaration)
    assert corrected.decision == declaration
    invoke_tool(comms, "comms_decision", {
        **request, "chosen": "/artifacts/peer-later",
        "change": {"kind": "correction", "original": first["reference"]},
    })
    snapshot = comms.registry.snapshot()
    with comms.bus.log.locked():
        _, facts = comms.bus.log.compaction_messages_unlocked(
            owner.incarnation)
    retained = RetainedTaskFacts(facts).for_owner(owner, snapshot)
    assert len(retained.facts) == 3
    assert retained.current_decisions(owner, snapshot) == (corrected,)
    current, = tuple(fact for fact in retained.facts if isinstance(fact, CurrentUserDecisionCorrectionTaskFact))
    assert current.source == corrected
    assert FieldCodec.decode(RetainedTaskFacts, FieldCodec.encode(retained)) == retained
    assert original.decision.chosen in retained.text and original.decision.rejected[0] in retained.text
    from agent_comms.comms import Comms

    reopened = Comms(comms.root)
    assert reopened.bus.log.full_history()[1] == corrected


def test_original_goal_input_and_user_sources_share_compaction_fence(comms, monkeypatch):
    from agent_comms.compaction_boundary import CompactionBoundary
    from agent_comms.input_attempt import NotSentInput
    from agent_comms.input_disposition import FutureInputQueue, InputDispositions
    from agent_comms.owner_compaction_prepare import NativeWitness
    from agent_comms.retained_task_facts import CurrentDecisionTaskFact, GoalTaskFact, InputTaskFact, UserSourceTaskFact

    owner = admit(comms, "alpha")
    saved = comms.root / "source-only.jsonl"
    saved.write_text('{"type":"session","id":"source-only"}\n')
    owner = replace(owner, session_file=str(saved), goal=Goal("Keep exact /artifacts/root", "original-goal"))
    comms.registry.register(owner)
    user = comms.messaging.send_user_message("alpha", "Never replay UNKNOWN", worktree=str(comms.root))
    monkeypatch.setenv("PI_AGENT_ID", "alpha")
    choice = invoke_tool(comms, "comms_decision", {
        "chosen": "/artifacts/exact", "rejected": ["/scratch/guess"], "to": "#team",
    })
    inputs = InputDispositions(comms.root / InputDispositions.filename)
    historical = NotSentInput("acp:failed", None, "alpha", 1, "alpha", "original failed input")
    inputs.update(lambda document: replace(document, rows={historical.key: historical}))
    assert inputs.record("acp:future", seq=None, owner="alpha", admission=owner.active_turn.admission_generation,
                         target="alpha", text="queued later, not compacted earlier")
    document = inputs.read()

    class FutureQueue(FutureInputQueue):
        # A declared source-control receipt; no native/UI/queue acceptance claim.
        def future_inputs(self, owner, pending_input_key):
            return {"acp:future": document.rows["acp:future"]}

    witness = NativeWitness("source-only", str(saved), "leaf", "kept", "1:2:3:4:5")
    boundary = CompactionBoundary(comms.registry, inputs, FutureQueue())
    _, generation = comms.registry.live_owner_with_generation("alpha")
    before = (saved.read_bytes(), inputs.path.read_bytes())
    with boundary.hold(owner, generation, witness) as held:
        source = held.capture(None, None)
        source.require_current(held)
    assert next(fact.source for fact in source.retained.facts if isinstance(fact, UserSourceTaskFact)) == user
    assert next(fact.source for fact in source.retained.facts if isinstance(fact, GoalTaskFact)) == owner.goal
    assert next(fact.source.reference for fact in source.retained.facts
                if isinstance(fact, CurrentDecisionTaskFact)) == FieldCodec.decode(
                    type(user.reference), choice["reference"])
    assert tuple(fact.source for fact in source.retained.facts if isinstance(fact, InputTaskFact)) == (historical,)
    assert (saved.read_bytes(), inputs.path.read_bytes()) == before
    updated = replace(owner, goal=Goal("Replacement goal", "new-goal"))
    comms.registry.register(updated)
    with boundary.hold(updated, generation, witness) as held:
        with pytest.raises(RelationViolationError, match="source changed"):
            source.require_current(held)
    assert (saved.read_bytes(), inputs.path.read_bytes()) == before
