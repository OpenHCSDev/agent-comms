"""One original decision/correction journey through declared tools and wire custody."""

import os
import json
from pathlib import Path
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import Goal
from agent_comms.messages import Message, MessageType
from agent_comms.task_sources import Decision
from agent_comms.thread_identity import TurnId
from agent_comms.threads import Thread
from agent_comms.tools import invoke_tool, tool_catalog


def saved_source(path):
    """A genuine owned saved-source fixture; no fabricated revision witness."""
    rows = [
        {"type": "session", "id": "source-only", "version": 3, "cwd": str(path.parent)},
        {"type": "message", "id": "kept", "parentId": None,
         "message": {"role": "user", "content": [{"type": "text", "text": "Original source"}]}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)


def source_witness(path):
    from agent_comms.owner_compaction_prepare import NativeWitness
    from agent_comms.private_path import FileRevision

    return NativeWitness("source-only", str(path), "kept", "kept",
                         FileRevision.from_stat(path.stat()))


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


def test_user_pin_original_source_without_model_lease_and_complete_lineage(comms):
    from agent_comms.retained_task_facts import CurrentHumanConstraintTaskFact, RetainedTaskFacts
    from agent_comms.task_sources import (
        CorrectionTaskChange, ExplicitTaskScopeSelection, HumanConstraintPin,
        TurnTaskScope, UserTaskDrop, UserTaskSupersession,
    )

    comms.messaging.initialize_private_initial_protocol()
    owner = comms.registry.declare(Thread("recipient", frozenset({"team"}), str(comms.root)))
    comms.registry.declare(Thread("unaddressed", frozenset(), str(comms.root)))
    exact = "Never replay UNKNOWN.\nUse original λ /artifacts/source; no substitute."
    subject = comms.messaging.send_user_message("recipient", exact, worktree=owner.worktree)
    original_bytes = (comms.root / "bus.jsonl").read_bytes()
    assert owner.turn_lease is None
    pin = comms.messaging.pin_user_constraint(owner.name, subject.reference, worktree=owner.worktree)
    assert isinstance(pin.task, HumanConstraintPin)
    assert pin.notice and not pin.starts_turn and pin.task.subject == subject.reference
    assert exact not in pin.body and "source_turn" not in FieldCodec.encode(pin.task)

    def captured(name):
        current = comms.registry.require(name)
        with comms.bus.log.certified_read() as source:
            facts = tuple(source.retained_task_facts(current.incarnation))
        return RetainedTaskFacts(facts).for_owner(current, comms.registry.snapshot())

    retained = captured(owner.name)
    assert retained.current_authored_sources(owner, comms.registry.snapshot()) == (pin,)
    assert retained.original_text_source(pin) == subject
    assert sum(f.source.reference == subject.reference for f in retained.facts) == 1
    assert sum(f.source.reference == pin.reference for f in retained.facts) == 1
    assert any(isinstance(f, CurrentHumanConstraintTaskFact) for f in retained.facts)
    assert retained.text.count(exact.replace("\n", "\\n")) == 1
    assert FieldCodec.decode(RetainedTaskFacts, FieldCodec.encode(retained)) == retained
    assert (comms.root / "bus.jsonl").read_bytes().startswith(original_bytes)
    before = (comms.root / "bus.jsonl").read_bytes()
    with pytest.raises(RelationViolationError, match="did not receive"):
        comms.messaging.pin_user_constraint("unaddressed", subject.reference, worktree=owner.worktree)
    with pytest.raises(RelationViolationError, match="model turn scope"):
        comms.messaging.pin_user_constraint(owner.name, subject.reference, worktree=owner.worktree,
            scope=ExplicitTaskScopeSelection(TurnTaskScope(project=owner.worktree)))
    comms.registry.declare(Thread("peer", frozenset(), str(comms.root)))
    with pytest.raises(RelationViolationError, match="author"):
        comms.messaging.send_message("peer", owner.name, "claim human source", task=pin.task)
    assert (comms.root / "bus.jsonl").read_bytes() == before
    comms.registry.rename(owner.name, "renamed-recipient")
    renamed = comms.registry.require("renamed-recipient")
    assert captured(renamed.name).current_authored_sources(renamed, comms.registry.snapshot()) == (pin,)
    revised = comms.messaging.send_user_message(renamed.name, "Keep the corrected original path.",
        worktree=owner.worktree, task=UserTaskSupersession(CorrectionTaskChange(pin.reference)))
    assert captured(renamed.name).current_authored_sources(renamed, comms.registry.snapshot()) == (revised,)
    comms.messaging.send_user_message(renamed.name, "Drop that constraint.", worktree=owner.worktree,
        task=UserTaskDrop(CorrectionTaskChange(pin.reference)))
    assert captured(renamed.name).current_authored_sources(renamed, comms.registry.snapshot()) == ()
    # A new declaration can refer to equal wording only through its own identity.
    second = comms.messaging.send_user_message(renamed.name, exact, worktree=owner.worktree)
    new_pin = comms.messaging.pin_user_constraint(renamed.name, second.reference, worktree=owner.worktree)
    current = captured(renamed.name)
    assert current.original_text_source(new_pin) == second and second.reference != subject.reference
    assert current.current_authored_sources(renamed, comms.registry.snapshot()) == (new_pin,)
    repeated_pin = comms.messaging.pin_user_constraint(renamed.name, second.reference, worktree=owner.worktree)
    assert captured(renamed.name).current_authored_sources(renamed, comms.registry.snapshot()) == (repeated_pin,)
    assert current.original_text_source(new_pin) == second
    comms.registry.register(replace(renamed, worktree=str(comms.root / "new-project")))
    assert captured(renamed.name).current_authored_sources(comms.registry.require(renamed.name),
                                                        comms.registry.snapshot()) == ()


def test_user_pin_goal_scope_replacement_and_original_subject_fences(comms):
    from agent_comms.task_sources import CorrectionTaskChange, GoalTaskScope

    comms.messaging.initialize_private_initial_protocol()
    owner = comms.registry.declare(Thread("recipient", frozenset(), str(comms.root),
                                         goal=Goal("Original goal", "goal-one")))
    subject = comms.messaging.send_user_message(owner.name, "Use the original coordinates.",
                                                worktree=owner.worktree)
    pin = comms.messaging.pin_user_constraint(owner.name, subject.reference, worktree=owner.worktree)
    assert isinstance(pin.task.scope, GoalTaskScope)
    before = (comms.root / "bus.jsonl").read_bytes()
    with pytest.raises(RelationViolationError, match="original source"):
        comms.messaging.pin_user_constraint(owner.name,
            replace(subject.reference, message_id="foreign-original"), worktree=owner.worktree)
    with pytest.raises(RelationViolationError, match="original recipient"):
        comms.messaging.send_user_message("#team", "wrong audience", worktree=owner.worktree, task=pin.task)
    assert (comms.root / "bus.jsonl").read_bytes() == before
    comms.registry.register(replace(owner, goal=Goal("Replacement goal", "goal-two")))
    new_subject = comms.messaging.send_user_message(owner.name, "Use the replacement coordinates.",
                                                    worktree=owner.worktree)
    with pytest.raises(RelationViolationError, match="original scope"):
        comms.messaging.pin_user_constraint(owner.name, new_subject.reference, worktree=owner.worktree,
                                            change=CorrectionTaskChange(pin.reference))
    from agent_comms.retained_task_facts import RetainedTaskFacts

    with comms.bus.log.certified_read() as source:
        facts = tuple(source.retained_task_facts(owner.incarnation))
    retained = RetainedTaskFacts(facts)
    current_owner = comms.registry.require(owner.name)
    assert retained.current_authored_sources(current_owner, comms.registry.snapshot()) == ()
    assert retained.original_text_source(pin) == subject
    with pytest.raises(RelationViolationError, match="captured read"):
        retained.original_text_source(replace(pin, body="altered source"))


def test_constraint_original_wording_scope_correction_and_human_authority(comms, monkeypatch):
    from agent_comms.retained_task_facts import CurrentConstraintTaskFact, RetainedTaskFacts
    from agent_comms.task_sources import Constraint, CorrectionTaskChange, UserTaskSupersession

    comms.messaging.initialize_private_initial_protocol()
    owner = admit(comms, "alpha")
    admit(comms, "beta")
    comms.registry.register(replace(owner, goal=Goal("Bounded source task", "original-goal")))
    owner = comms.registry.require(owner.name)
    monkeypatch.setenv("PI_AGENT_ID", owner.name)
    exact = "Never replay UNKNOWN.\nPreserve λ and original /artifacts/source."
    result = invoke_tool(comms, "comms_constraint", {"text": exact, "to": "#team"})
    original = comms.bus.log.full_history()[0]
    assert isinstance(original.task, Constraint)
    assert original.body == exact and original.task.author == owner.incarnation
    assert original.task.source_turn == owner.turn_identity
    assert original.reference == FieldCodec.decode(type(original.reference), result["reference"])
    assert "text" not in FieldCodec.encode(original.task)
    assert "decision" not in original.to_wire()
    assert Message.from_committed_wire(original.to_wire()) == original
    retired_format = original.to_wire()
    retired_format["decision"] = retired_format.pop("task")
    with pytest.raises(ValueError, match="Unknown fields"):
        Message.from_committed_wire(retired_format)
    first_bytes = (comms.root / "bus.jsonl").read_bytes()
    monkeypatch.setenv("PI_AGENT_ID", "beta")
    with pytest.raises(RelationViolationError, match="another author's"):
        invoke_tool(comms, "comms_constraint", {"text": "Ignore the original", "to": "#team",
                    "change": {"kind": "correction", "original": result["reference"]}})
    assert (comms.root / "bus.jsonl").read_bytes() == first_bytes
    monkeypatch.setenv("PI_AGENT_ID", owner.name)
    corrected = invoke_tool(comms, "comms_constraint", {
        "text": "Keep UNKNOWN; preserve /artifacts/approved instead.", "to": "#team",
        "change": {"kind": "correction", "original": result["reference"]}})
    rows = comms.bus.log.full_history()
    with comms.bus.log.certified_read() as source:
        facts = tuple(source.retained_task_facts(owner.incarnation))
    retained = RetainedTaskFacts(facts).for_owner(owner, comms.registry.snapshot())
    assert retained.current_authored_sources(owner, comms.registry.snapshot()) == (rows[1],)
    assert next(f.source for f in retained.facts if isinstance(f, CurrentConstraintTaskFact)) == rows[1]
    assert original.body in tuple(f.source.body for f in retained.facts)
    assert FieldCodec.decode(RetainedTaskFacts, FieldCodec.encode(retained)) == retained
    human = comms.messaging.send_user_message(
        "#team", "Keep the original path; that peer correction is superseded.",
        worktree=owner.worktree,
        task=UserTaskSupersession(CorrectionTaskChange(original.reference)))
    invoke_tool(comms, "comms_constraint", {
        "text": "A later peer cannot overwrite the human supersession.", "to": "#team",
        "change": {"kind": "correction", "original": corrected["reference"]}})
    with comms.bus.log.certified_read() as source:
        facts = tuple(source.retained_task_facts(owner.incarnation))
    retained = RetainedTaskFacts(facts).for_owner(owner, comms.registry.snapshot())
    assert retained.current_authored_sources(owner, comms.registry.snapshot()) == (human,)
    retained.require_summary(retained.text + "\n\nOptional narrative")
    comms.registry.rename(owner.name, "renamed-alpha")
    renamed = comms.registry.require("renamed-alpha")
    assert retained.current_authored_sources(renamed, comms.registry.snapshot()) == (human,)
    comms.registry.register(replace(renamed, goal=Goal("Replacement scope", "new-goal")))
    assert retained.current_authored_sources(comms.registry.require(renamed.name),
                                            comms.registry.snapshot()) == ()
    assert comms.bus.log.full_history()[0] == original


def test_original_choice_correction_and_authority_survive_reopen(comms, monkeypatch):
    alpha = admit(comms, "alpha")
    beta = admit(comms, "beta")
    monkeypatch.setenv("PI_AGENT_ID", "alpha")
    request = {"chosen": "Keep /artifacts/exact-root", "rejected": ["Use /scratch/guess-root"], "to": "#team"}
    first = invoke_tool(comms, "comms_decision", request)
    original = comms.bus.log.full_history()[0]
    assert original.reference == FieldCodec.decode(type(original.reference), first["reference"])
    assert original.task.author == alpha.incarnation
    assert original.task.source_turn == alpha.turn_identity
    assert original.notice and not original.starts_turn
    assert Message.from_committed_wire(original.to_wire()) == original
    correction = invoke_tool(comms, "comms_decision", {
        **request, "chosen": "Keep /artifacts/certified-root", "change": {"kind": "correction", "original": first["reference"]},
    })
    rows = comms.bus.log.full_history()
    assert len(rows) == 2 and rows[0] == original
    assert rows[1].task.change.original == original.reference
    assert correction["reference"] == FieldCodec.encode(rows[1].reference)
    from agent_comms.retained_task_facts import RetainedTaskFacts

    with comms.bus.log.certified_read() as source:
        facts = tuple(source.retained_task_facts(alpha.incarnation))
    retained = RetainedTaskFacts(facts)
    assert tuple(fact.source.reference for fact in retained.facts) == tuple(
        row.reference for row in rows
    )
    assert FieldCodec.decode(RetainedTaskFacts, FieldCodec.encode(retained)) == retained
    assert original.task.chosen in retained.text
    assert original.task.rejected[0] in retained.text
    assert rows[1].task.change.original.message_id in retained.text
    assert retained.current_authored_sources(alpha, comms.registry.snapshot()) == (rows[1],)
    with pytest.raises(RelationViolationError, match="omitted"):
        retained.require_summary("Assistant prose cannot replace original authored_sources")
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
    assert retained.current_authored_sources(renamed, comms.registry.snapshot()) == (rows[1],)
    monkeypatch.setenv("PI_AGENT_ID", "renamed-alpha")
    comms.registry.release_turn(alpha.turn_lease)
    assert retained.current_authored_sources(comms.registry.require("alpha"), comms.registry.snapshot()) == ()
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
    with comms.bus.log.certified_read() as source:
        facts = tuple(source.retained_task_facts(renamed.incarnation))
    retained = RetainedTaskFacts(facts).for_owner(renamed, snapshot)
    rows = comms.bus.log.full_history()
    assert tuple(fact.source.reference for fact in retained.facts) == tuple(row.reference for row in rows)
    assert retained.current_authored_sources(renamed, snapshot) == (rows[2], rows[1])
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
    assert retained.current_authored_sources(renamed, comms.registry.snapshot()) == ()
    assert (comms.root / "bus.jsonl").read_bytes() == before


def test_stale_goal_scope_cannot_publish_even_through_original_publisher(comms):
    owner = admit(comms, "alpha")
    owner = replace(owner, goal=Goal("Keep exact scope", "goal", revision=1))
    comms.registry.register(owner)
    declaration = Decision(
        chosen="A", rejected=("B",), scope=owner.task_scope,
        source_turn=owner.turn_identity,
        source_turn_id=TurnId(owner.active_turn.id),
    )
    comms.registry.register(replace(owner, goal=replace(owner.goal, revision=2)))
    message = Message("alpha", "#team", declaration.text, MessageType.INFO, task=declaration)
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
    from agent_comms.retained_task_facts import CurrentUserTaskCorrectionFact, RetainedTaskFacts
    from agent_comms.task_sources import CorrectionTaskChange, UserTaskSupersession

    owner = admit(comms, "alpha")
    owner = replace(owner, goal=Goal("Keep project scope", "goal", revision=1))
    comms.registry.register(owner)
    monkeypatch.setenv("PI_AGENT_ID", "alpha")
    request = {"chosen": "/artifacts/old", "rejected": ["/scratch/guess"], "to": "#team"}
    first = invoke_tool(comms, "comms_decision", request)
    original = comms.bus.log.full_history()[0]
    declaration = UserTaskSupersession(CorrectionTaskChange(original.reference))
    before = (comms.root / "bus.jsonl").read_bytes()
    with pytest.raises(RelationViolationError, match="human sender"):
        comms.bus.publisher.publish_ordinary(
            Message(owner.name, "#team", "A peer is not the user", MessageType.INFO, task=declaration))
    assert (comms.root / "bus.jsonl").read_bytes() == before
    corrected = comms.messaging.send_user_message(
        "#team", "Retract the old path choice. Never replay UNKNOWN.",
        worktree=owner.worktree, task=declaration)
    assert corrected.task == declaration
    invoke_tool(comms, "comms_decision", {
        **request, "chosen": "/artifacts/peer-later",
        "change": {"kind": "correction", "original": first["reference"]},
    })
    snapshot = comms.registry.snapshot()
    with comms.bus.log.certified_read() as source:
        facts = tuple(source.retained_task_facts(owner.incarnation))
    retained = RetainedTaskFacts(facts).for_owner(owner, snapshot)
    assert len(retained.facts) == 3
    assert retained.current_authored_sources(owner, snapshot) == (corrected,)
    current, = tuple(fact for fact in retained.facts if isinstance(fact, CurrentUserTaskCorrectionFact))
    assert current.source == corrected
    assert FieldCodec.decode(RetainedTaskFacts, FieldCodec.encode(retained)) == retained
    assert original.task.chosen in retained.text and original.task.rejected[0] in retained.text
    from agent_comms.comms import Comms

    reopened = Comms(comms.root)
    assert reopened.bus.log.full_history()[1] == corrected


def test_original_goal_input_and_user_sources_share_compaction_fence(comms, monkeypatch):
    from agent_comms.compaction_boundary import CompactionBoundary
    from agent_comms.input_attempt import NotSentInput
    from agent_comms.input_disposition import FutureInputQueue, InputDispositions
    from agent_comms.owner_compaction_prepare import NativeWitness
    from agent_comms.retained_task_facts import CurrentDecisionTaskFact, GoalTaskFact, InputTaskFact, UserSourceTaskFact

    comms.messaging.initialize_private_initial_protocol()
    owner = admit(comms, "alpha")
    saved = comms.root / "source-only.jsonl"
    saved_source(saved)
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
        def future_inputs(self, owner, pending_input_keys):
            return {"acp:future": document.rows["acp:future"]}

    witness = source_witness(saved)
    boundary = CompactionBoundary(comms.registry, inputs, FutureQueue())
    _, generation = comms.registry.live_owner_with_generation("alpha")
    before = (saved.read_bytes(), inputs.path.read_bytes())
    with boundary.hold(owner, generation, witness) as held:
        source = held.capture((), None)
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


def test_cross_audience_corrections_capture_only_eligible_owned_lineage(comms, monkeypatch):
    from agent_comms.compaction_boundary import CompactionBoundary
    from agent_comms.input_disposition import FutureInputQueue, InputDispositions
    from agent_comms.owner_compaction_prepare import NativeWitness
    from agent_comms.retained_task_facts import (
        CurrentDecisionTaskFact, CurrentUserTaskCorrectionFact,
        DecisionTaskFact, RetainedTaskFacts,
    )
    from agent_comms.task_sources import CorrectionTaskChange, UserTaskSupersession

    comms.messaging.initialize_private_initial_protocol()
    for name in ("alpha", "beta", "gamma"):
        owner = admit(comms, name)
        saved = comms.root / f"{name}-source.jsonl"
        saved_source(saved)
        comms.registry.register(replace(owner, session_file=str(saved)))
    owner = comms.registry.require("alpha")
    monkeypatch.setenv("PI_AGENT_ID", "alpha")
    request = {"chosen": "PRIVATE_ORIGINAL_CHOICE", "rejected": ["PRIVATE_ALTERNATIVE"], "to": "beta"}
    first = invoke_tool(comms, "comms_decision", request)
    original = comms.bus.log.full_history()[0]
    supersession = comms.messaging.send_user_message(
        "#team", "Retract that declared choice, without revealing its private text.",
        worktree=owner.worktree,
        task=UserTaskSupersession(CorrectionTaskChange(original.reference)),
    )
    invoke_tool(comms, "comms_decision", {
        **request, "chosen": "PUBLIC_LATER_CHOICE", "rejected": ["PUBLIC_ALTERNATIVE"],
        "to": "#team", "change": {"kind": "correction", "original": first["reference"]},
    })
    later = comms.bus.log.full_history()[-1]
    wire_before = (comms.root / "bus.jsonl").read_bytes()
    inputs = InputDispositions(comms.root / InputDispositions.filename)

    class EmptyFutureQueue(FutureInputQueue):
        def future_inputs(self, owner, pending_input_keys):
            return {}

    boundary = CompactionBoundary(comms.registry, inputs, EmptyFutureQueue())
    captured = {}
    for name in ("alpha", "beta", "gamma"):
        current = comms.registry.require(name)
        _, generation = comms.registry.live_owner_with_generation(name)
        witness = source_witness(Path(current.session_file))
        with boundary.hold(current, generation, witness) as held:
            source = held.capture((), None)
            source.require_current(held)
        captured[name] = source.retained
    snapshot = comms.registry.snapshot()
    assert captured["alpha"].current_authored_sources(comms.registry.require("alpha"), snapshot) == (supersession,)
    assert captured["beta"].current_authored_sources(comms.registry.require("beta"), snapshot) == ()
    outsider = captured["gamma"]
    assert outsider.current_authored_sources(comms.registry.require("gamma"), snapshot) == ()
    assert tuple(fact.source for fact in outsider.facts) == (supersession, later)
    assert not any(isinstance(fact, (CurrentDecisionTaskFact, CurrentUserTaskCorrectionFact))
                   for fact in outsider.facts)
    assert "PRIVATE_ORIGINAL_CHOICE" not in outsider.text
    assert "PRIVATE_ALTERNATIVE" not in outsider.text
    assert FieldCodec.decode(RetainedTaskFacts, FieldCodec.encode(outsider)) == outsider
    assert (comms.root / "bus.jsonl").read_bytes() == wire_before
    # A malformed cut omitting this owner's applicable original still refuses;
    # neutral foreign history is not a generic missing-source fallback.
    with pytest.raises(RelationViolationError, match="lacks its original"):
        RetainedTaskFacts((DecisionTaskFact(later),)).current_authored_sources(
            comms.registry.require("alpha"), snapshot)


@pytest.mark.asyncio
async def test_explicit_subtask_observation_correction_drop_and_turn_continuity(comms, monkeypatch):
    import threading
    from agent_comms.owner_compaction_settings import PiCompactionDecision
    from agent_comms.pi_vocabulary import (
        ManualCompactionReason, OverflowCompactionReason,
        TaskBoundaryCompactionReason, ThresholdCompactionReason,
    )
    from agent_comms.retained_task_facts import RetainedTaskFacts
    from agent_comms.task_sources import CorrectionTaskChange, UserTaskDrop

    comms.messaging.initialize_private_initial_protocol()
    owner = admit(comms, "alpha")
    monkeypatch.setenv("PI_AGENT_ID", owner.name)

    def boundary():
        snapshot = comms.registry.snapshot()
        current = snapshot.require(owner.name)
        retained = comms.bus.log.retained_context(owner.name, comms.registry).retained
        assert FieldCodec.decode(RetainedTaskFacts, FieldCodec.encode(retained)) == retained
        return retained.optional_boundary(current, snapshot)

    assert boundary() == ()
    request = {"text": "Original independent subtask", "completed": True, "to": "#team"}
    first = invoke_tool(comms, "comms_subtask", request)
    original = comms.bus.log.full_history()[0]
    assert boundary() == (original.reference,)
    assert original.body == request["text"]
    assert original.task.source_turn == owner.turn_identity
    retained = comms.bus.log.retained_context(owner.name, comms.registry).retained
    loop_thread = threading.get_ident()
    reads = []
    registry_type = type(comms.registry)
    snapshot = registry_type.snapshot

    def read_snapshot(registry):
        assert threading.get_ident() != loop_thread, "Registry read blocked the async owner"
        reads.append(threading.get_ident())
        return snapshot(registry)

    with monkeypatch.context() as patch:
        patch.setattr(registry_type, "snapshot", read_snapshot)
        for reason in (ManualCompactionReason, OverflowCompactionReason, ThresholdCompactionReason):
            decision = PiCompactionDecision(2048, 1000, True, True, reason, ())
            assert await decision.boundary_current(retained, owner, comms.registry)
        optional = PiCompactionDecision(2048, 1000, True, True, TaskBoundaryCompactionReason, ())
        assert not await optional.boundary_current(retained, owner, comms.registry)
        assert reads == []
        optional = replace(optional, boundary=(original.reference,))
        assert await optional.boundary_current(retained, owner, comms.registry)
        assert len(reads) == 1
        assert not await replace(optional, boundary=optional.boundary * 2).boundary_current(
            retained, owner, comms.registry)
        assert len(reads) == 2
    unfinished = invoke_tool(comms, "comms_subtask", {
        **request, "completed": False,
        "change": {"kind": "correction", "original": first["reference"]}})
    assert boundary() == ()
    completed = invoke_tool(comms, "comms_subtask", {
        **request, "change": {"kind": "correction", "original": unfinished["reference"]}})
    last = comms.bus.log.full_history()[-1]
    assert boundary() == (last.reference,)
    comms.messaging.send_user_message("#team", "Withdraw that completed observation.",
        worktree=owner.worktree, task=UserTaskDrop(CorrectionTaskChange(last.reference)))
    assert boundary() == ()  # Never fall back to an older completed observation.
    latest = invoke_tool(comms, "comms_subtask", request)
    latest_ref = comms.bus.log.full_history()[-1].reference
    comms.registry.release_turn(owner.turn_lease)
    assert boundary() == (latest_ref,)  # Finished turn is identity, not completion.
    current, admission = comms.registry.live_owner_with_admission(owner.name)
    next_owner, _ = comms.registry.lease_live_turn_with_admission(
        current, "next-original-turn", expected_generation=admission)
    assert boundary() == (latest_ref,)
    comms.registry.release_turn(next_owner.turn_lease)
    assert boundary() == ()  # The observation belongs to a different finished turn.
    assert comms.bus.log.full_history()[0] == original
    assert "comms_subtask" in {entry["name"] for entry in tool_catalog()}


def test_same_native_cut_policy_keeps_hard_preparation_independent(tmp_path):
    from agent_comms.owner_compaction_prepare import NativePreparation, SkipPreparationResult
    from agent_comms.owner_compaction_settings import PiCompactionDecision
    from agent_comms.pi_vocabulary import OverflowCompactionReason, TaskBoundaryCompactionReason
    from agent_comms.compaction_result import RefusedCompactionResult

    session = tmp_path / 'source.jsonl'
    saved_source(session)
    preparation = NativePreparation(source_witness(session), 8000, True)
    hard = PiCompactionDecision(2048, 1000, False, True, OverflowCompactionReason, ())
    optional = PiCompactionDecision(2048, 1000, True, True, TaskBoundaryCompactionReason, ())
    assert hard.prepare(preparation) is preparation
    assert isinstance(optional.prepare(preparation), SkipPreparationResult)
    assert hard.trigger and optional.trigger
    assert FieldCodec.decode(PiCompactionDecision, FieldCodec.encode(hard)) == hard
    refusal = RefusedCompactionResult('No complete optional cut')
    optional.require_prepared(refusal)
    with pytest.raises(ValueError, match='No complete optional cut'):
        hard.require_prepared(refusal)


@pytest.mark.asyncio
async def test_optional_clean_decline_has_no_write_or_input_grant():
    from types import SimpleNamespace
    from agent_comms.pi_summary_payloads import SummaryDeclinedData
    from agent_comms.pi_vocabulary import TaskBoundaryCompactionReason, ManualCompactionReason

    data = SummaryDeclinedData(version=1, operation_id='original-optional',
                               status='declined', reason='split_turn')
    settled = []
    refused = []
    journal = SimpleNamespace(summaries=SimpleNamespace(
        refuse=lambda operation, reason: refused.append((operation, reason))))
    result = data.manual_summary(journal, TaskBoundaryCompactionReason, settled.append)
    assert settled == [data] and not refused
    assert await result.commit_with(lambda _: pytest.fail('Optional decline wrote native history')) is None
    assert result.admit_original(None, None, None, None, None) is None
    assert result.compaction_result(None) is result and not result.adaptive_result()
    with pytest.raises(ValueError, match='declined manual summary'):
        data.manual_summary(journal, ManualCompactionReason, settled.append)
    assert refused == [(data.operation_id, data.reason)]
    unsafe = SummaryDeclinedData(version=1, operation_id='required-context',
                                 status='declined', reason='context_requires_compaction')
    with pytest.raises(ValueError, match='original remains unbound'):
        unsafe.manual_summary(journal, TaskBoundaryCompactionReason, settled.append)
    assert settled == [data]
