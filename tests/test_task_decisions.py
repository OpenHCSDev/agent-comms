"""One original decision/correction journey through declared tools and wire ownership."""

from agent_comms.bus_publication import AgentOrigin
from agent_comms.messages import NoticeRole
import os
import json
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import Goal
from agent_comms.messages import Message, InfoKind
from agent_comms.task_sources import Decision
from agent_comms.thread_identity import TurnId
from agent_comms.threads import Thread
from agent_comms.tools import invoke_tool
from agent_comms.native_tools import NativeRequestTools
from agent_comms.tool_caller import ThreadCaller
from functools import partial
from bus_history import read_history

# These source declarations belong to alpha unless a case explicitly supplies beta.
invoke_tool = partial(invoke_tool, caller=ThreadCaller("alpha"))


def saved_source(path):
    """A genuine owned saved-source fixture; no fabricated revision witness."""
    rows = [
        {"type": "session", "id": "source-only", "version": 3, "cwd": str(path.parent)},
        {"type": "message", "id": "kept", "parentId": None,
         "message": {"role": "user", "content": [{"type": "text", "text": "Original source"}]}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)


def admit(comms, name):
    comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(comms.root), process_identity=ProcessIdentity.capture(os.getpid())))
    owner, claim_scope = comms.registry.live_owner_with_claim(name)
    owner, _ = comms.registry.lease_live_turn_with_claim(
        owner, name + "-turn", expected_generation=claim_scope
    )
    return owner


def test_user_pin_original_source_without_model_lease_and_complete_lineage(comms):
    from agent_comms.retained_task_facts import CurrentHumanConstraintTaskFact, RetainedTaskFacts
    from agent_comms.task_sources import (
        CorrectionTaskChange, ExplicitTaskScopeSelection, HumanConstraintPin,
        TurnTaskScope, UserTaskDrop, UserTaskSupersession,
    )

    comms.messaging.initialize_private_initial_protocol()
    owner = comms.registry.declare(comms.registry.new_thread('recipient', frozenset({'team'}), str(comms.root)))
    comms.registry.declare(comms.registry.new_thread('unaddressed', frozenset(), str(comms.root)))
    exact = "Never replay UNKNOWN.\nUse original λ /artifacts/source; no substitute."
    subject = comms.messaging.send_user_message("recipient", exact, worktree=owner.worktree)
    original_bytes = (comms.root / "bus.jsonl").read_bytes()
    assert owner.turn_lease is None
    pin = comms.messaging.pin_user_constraint(owner.name, subject.reference, worktree=owner.worktree)
    assert isinstance(pin.task, HumanConstraintPin)
    assert isinstance(pin.role, NoticeRole) and not pin.role.response_policy(pin).starts_turn and pin.task.subject == subject.reference
    assert exact not in pin.body and "source_turn" not in FieldCodec.encode(pin.task)

    def captured(name):
        current = comms.registry.require(name)
        with comms.bus.log.checked_read() as source:
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
    comms.registry.declare(comms.registry.new_thread('peer', frozenset(), str(comms.root)))
    with pytest.raises(RelationViolationError, match="author"):
        comms.messaging.send("peer", owner.name, "claim human source", task=pin.task).message
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
    owner = comms.registry.declare(comms.registry.new_thread('recipient', frozenset(), str(comms.root), goal=Goal('Original goal', 'goal-one', published_seq=0)))
    subject = comms.messaging.send_user_message(owner.name, "Use the original coordinates.",
                                                worktree=owner.worktree)
    pin = comms.messaging.pin_user_constraint(owner.name, subject.reference, worktree=owner.worktree)
    assert isinstance(pin.task.scope, GoalTaskScope)
    before = (comms.root / "bus.jsonl").read_bytes()
    with pytest.raises(RelationViolationError, match="original source"):
        comms.messaging.pin_user_constraint(owner.name,
            replace(subject.reference, message_id="foreign-original"), worktree=owner.worktree)
    from conftest import declare_test_bus
    declare_test_bus(comms, channels=("team",))
    with pytest.raises(RelationViolationError, match="original recipient"):
        comms.messaging.send_user_message("#team", "wrong audience", worktree=owner.worktree, task=pin.task)
    assert (comms.root / "bus.jsonl").read_bytes() == before
    comms.registry.register(replace(owner, goal=Goal("Replacement goal", "goal-two", published_seq=0)))
    new_subject = comms.messaging.send_user_message(owner.name, "Use the replacement coordinates.",
                                                    worktree=owner.worktree)
    with pytest.raises(RelationViolationError, match="original scope"):
        comms.messaging.pin_user_constraint(owner.name, new_subject.reference, worktree=owner.worktree,
                                            change=CorrectionTaskChange(pin.reference))
    from agent_comms.retained_task_facts import RetainedTaskFacts

    with comms.bus.log.checked_read() as source:
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
    comms.registry.register(replace(owner, goal=Goal("Bounded source task", "original-goal", published_seq=0)))
    owner = comms.registry.require(owner.name)
    monkeypatch.setenv("AGENT_COMMS_THREAD", owner.name)
    exact = "Never replay UNKNOWN.\nPreserve λ and original /artifacts/source."
    result = invoke_tool(comms, "comms_constraint", {"text": exact, "to": "#team"})
    original = read_history(comms)[0]
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
    monkeypatch.setenv("AGENT_COMMS_THREAD", "beta")
    with pytest.raises(RelationViolationError, match="another author's"):
        invoke_tool(comms, "comms_constraint", {"text": "Ignore the original", "to": "#team",
                    "change": {"kind": "correction", "original": result["reference"]}}, caller=ThreadCaller("beta"))
    assert (comms.root / "bus.jsonl").read_bytes() == first_bytes
    monkeypatch.setenv("AGENT_COMMS_THREAD", owner.name)
    corrected = invoke_tool(comms, "comms_constraint", {
        "text": "Keep UNKNOWN; preserve /artifacts/approved instead.", "to": "#team",
        "change": {"kind": "correction", "original": result["reference"]}})
    rows = read_history(comms)
    with comms.bus.log.checked_read() as source:
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
    with comms.bus.log.checked_read() as source:
        facts = tuple(source.retained_task_facts(owner.incarnation))
    retained = RetainedTaskFacts(facts).for_owner(owner, comms.registry.snapshot())
    assert retained.current_authored_sources(owner, comms.registry.snapshot()) == (human,)
    comms.registry.rename(owner.name, "renamed-alpha")
    renamed = comms.registry.require("renamed-alpha")
    assert retained.current_authored_sources(renamed, comms.registry.snapshot()) == (human,)
    comms.registry.register(replace(renamed, goal=Goal("Replacement scope", "new-goal", published_seq=0)))
    assert retained.current_authored_sources(comms.registry.require(renamed.name),
                                            comms.registry.snapshot()) == ()
    assert read_history(comms)[0] == original


def test_original_choice_correction_and_authority_survive_reopen(comms, monkeypatch):
    comms.messaging.initialize_private_initial_protocol()
    alpha = admit(comms, "alpha")
    beta = admit(comms, "beta")
    monkeypatch.setenv("AGENT_COMMS_THREAD", "alpha")
    request = {"chosen": "Keep /artifacts/exact-root", "rejected": ["Use /scratch/guess-root"], "to": "#team"}
    first = invoke_tool(comms, "comms_decision", request)
    original = read_history(comms)[0]
    assert original.reference == FieldCodec.decode(type(original.reference), first["reference"])
    assert original.task.author == alpha.incarnation
    assert original.task.source_turn == alpha.turn_identity
    assert isinstance(original.role, NoticeRole) and not original.role.response_policy(original).starts_turn
    assert Message.from_committed_wire(original.to_wire()) == original
    correction = invoke_tool(comms, "comms_decision", {
        **request, "chosen": "Keep /artifacts/checked-root", "change": {"kind": "correction", "original": first["reference"]},
    })
    rows = read_history(comms)
    assert len(rows) == 2 and rows[0] == original
    assert rows[1].task.change.original == original.reference
    assert correction["reference"] == FieldCodec.encode(rows[1].reference)
    from agent_comms.retained_task_facts import RetainedTaskFacts

    with comms.bus.log.checked_read() as source:
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
    bus = comms.root / "bus.jsonl"
    before = bus.read_bytes()
    monkeypatch.setenv("AGENT_COMMS_THREAD", "beta")
    with pytest.raises(RelationViolationError, match="another author's"):
        invoke_tool(comms, "comms_decision", {**request, "change": {"kind": "correction", "original": first["reference"]}}, caller=ThreadCaller("beta"))
    assert bus.read_bytes() == before
    assert comms.registry.require("beta").turn_lease == beta.turn_lease
    monkeypatch.setenv("AGENT_COMMS_THREAD", "alpha")
    with pytest.raises(ValueError, match="Unknown fields"):
        invoke_tool(comms, "comms_decision", {**request, "author": "beta"})
    comms.registry.rename("alpha", "renamed-alpha")
    renamed = comms.registry.require("renamed-alpha")
    assert retained.current_authored_sources(renamed, comms.registry.snapshot()) == (rows[1],)
    monkeypatch.setenv("AGENT_COMMS_THREAD", "renamed-alpha")
    comms.registry.release_turn(alpha.turn_lease)
    assert retained.current_authored_sources(comms.registry.require("alpha"), comms.registry.snapshot()) == ()
    with pytest.raises(ValueError, match="authorized"):
        invoke_tool(comms, "comms_decision", request)
    assert bus.read_bytes() == before
    from agent_comms.comms import Comms

    assert read_history(Comms(comms.root)) == rows
    schema = next(tool for tool in NativeRequestTools.catalog() if tool["name"] == "comms_decision")
    assert set(schema["parameters"]["required"]) == {"chosen", "rejected", "to"}
    assert "author" not in schema["parameters"]["properties"]


def test_equal_text_choices_keep_original_identity_through_rename_and_goal_replacement(comms, monkeypatch):
    from agent_comms.retained_task_facts import CurrentDecisionTaskFact, RetainedTaskFacts

    comms.messaging.initialize_private_initial_protocol()
    owner = admit(comms, "alpha")
    owner = replace(owner, goal=Goal("Keep checked root", "goal", published_seq=0, revision=1))
    comms.registry.register(owner)
    monkeypatch.setenv("AGENT_COMMS_THREAD", "alpha")
    request = {"chosen": "/artifacts/exact", "rejected": ["/scratch/guess"], "to": "#team"}
    first = invoke_tool(comms, "comms_decision", request)
    invoke_tool(comms, "comms_decision", request)
    originals = read_history(comms)
    assert originals[0].body == originals[1].body
    assert originals[0].reference != originals[1].reference
    comms.registry.rename("alpha", "renamed-alpha")
    renamed = comms.registry.require("renamed-alpha")
    monkeypatch.setenv("AGENT_COMMS_THREAD", "renamed-alpha")
    invoke_tool(comms, "comms_decision", {
        **request, "chosen": "/artifacts/checked",
        "change": {"kind": "correction", "original": first["reference"]},
    })
    snapshot = comms.registry.snapshot()
    with comms.bus.log.checked_read() as source:
        facts = tuple(source.retained_task_facts(renamed.incarnation))
    retained = RetainedTaskFacts(facts).for_owner(renamed, snapshot)
    rows = read_history(comms)
    assert tuple(fact.source.reference for fact in retained.facts) == tuple(row.reference for row in rows)
    assert retained.current_authored_sources(renamed, snapshot) == (rows[2], rows[1])
    assert tuple(fact.source.reference for fact in retained.facts
                 if isinstance(fact, CurrentDecisionTaskFact)) == (rows[1].reference, rows[2].reference)
    assert FieldCodec.decode(RetainedTaskFacts, FieldCodec.encode(retained)) == retained
    before = (comms.root / "bus.jsonl").read_bytes()
    replacement = replace(renamed, goal=Goal("New task", "replacement", published_seq=0, revision=1))
    comms.registry.register(replacement)
    stale = retained.for_owner(replacement, comms.registry.snapshot())
    assert not any(isinstance(fact, CurrentDecisionTaskFact) for fact in stale.facts)
    assert tuple(fact.source for fact in stale.facts) == tuple(fact.source for fact in retained.facts)
    comms.registry.remove(renamed.name)
    assert retained.current_authored_sources(renamed, comms.registry.snapshot()) == ()
    assert (comms.root / "bus.jsonl").read_bytes() == before


def test_stale_goal_scope_cannot_publish_even_through_original_publisher(comms):
    owner = admit(comms, "alpha")
    owner = replace(owner, goal=Goal("Keep exact scope", "goal", published_seq=0, revision=1))
    comms.registry.register(owner)
    declaration = Decision(
        chosen="A", rejected=("B",), scope=owner.task_scope,
        source_turn=owner.turn_identity,
        source_turn_id=TurnId(owner.require_turn_lease().turn_id),
    )
    comms.registry.register(replace(owner, goal=replace(owner.goal, revision=2)))
    message = Message("alpha", "#team", declaration.text, InfoKind, task=declaration)
    with pytest.raises(ValueError, match="goal changed"):
        comms.bus.publisher.publish_ordinary(message, origin=AgentOrigin())
    assert not read_history(comms)


@pytest.mark.parametrize("chosen,rejected", [("", ("B",)), ("A", ()), ("A", ("A",)), ("A", ("B", "B"))])
def test_invalid_alternatives_do_not_publish(comms, monkeypatch, chosen, rejected):
    admit(comms, "alpha")
    monkeypatch.setenv("AGENT_COMMS_THREAD", "alpha")
    with pytest.raises(ValueError):
        invoke_tool(comms, "comms_decision", {"chosen": chosen, "rejected": list(rejected), "to": "#team"})
    assert not read_history(comms)


def test_turn_scope_correction_cannot_revive_a_finished_turn_choice(comms, monkeypatch):
    owner = admit(comms, "alpha")
    monkeypatch.setenv("AGENT_COMMS_THREAD", "alpha")
    request = {"chosen": "A", "rejected": ["B"], "to": "#team"}
    comms.messaging.initialize_private_initial_protocol()
    first = invoke_tool(comms, "comms_decision", request)
    comms.registry.release_turn(owner.turn_lease)
    owner, claim_scope = comms.registry.live_owner_with_claim("alpha")
    comms.registry.lease_live_turn_with_claim(owner, "next-turn", expected_generation=claim_scope)
    before = (comms.root / "bus.jsonl").read_bytes()
    with pytest.raises(RelationViolationError, match="different turn scope"):
        invoke_tool(comms, "comms_decision", {
            **request, "chosen": "C",
            "change": {"kind": "correction", "original": first["reference"]},
        })
    assert (comms.root / "bus.jsonl").read_bytes() == before


def test_exact_user_supersession_cannot_be_impersonated_or_overridden_by_peer_choice(comms, monkeypatch):
    comms.messaging.initialize_private_initial_protocol()
    from agent_comms.retained_task_facts import CurrentUserTaskCorrectionFact, RetainedTaskFacts
    from agent_comms.task_sources import CorrectionTaskChange, UserTaskSupersession

    owner = admit(comms, "alpha")
    owner = replace(owner, goal=Goal("Keep project scope", "goal", published_seq=0, revision=1))
    comms.registry.register(owner)
    monkeypatch.setenv("AGENT_COMMS_THREAD", "alpha")
    request = {"chosen": "/artifacts/old", "rejected": ["/scratch/guess"], "to": "#team"}
    first = invoke_tool(comms, "comms_decision", request)
    original = read_history(comms)[0]
    declaration = UserTaskSupersession(CorrectionTaskChange(original.reference))
    before = (comms.root / "bus.jsonl").read_bytes()
    with pytest.raises(RelationViolationError, match="human sender"):
        comms.bus.publisher.publish_ordinary(
            Message(owner.name, "#team", "A peer is not the user", InfoKind, task=declaration), origin=AgentOrigin())
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
    with comms.bus.log.checked_read() as source:
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
    assert read_history(reopened)[1] == corrected


def test_cross_audience_corrections_capture_only_eligible_owned_lineage(comms, monkeypatch):
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
    monkeypatch.setenv("AGENT_COMMS_THREAD", "alpha")
    request = {"chosen": "PRIVATE_ORIGINAL_CHOICE", "rejected": ["PRIVATE_ALTERNATIVE"], "to": "beta"}
    first = invoke_tool(comms, "comms_decision", request)
    original = read_history(comms)[0]
    supersession = comms.messaging.send_user_message(
        "#team", "Retract that declared choice, without revealing its private text.",
        worktree=owner.worktree,
        task=UserTaskSupersession(CorrectionTaskChange(original.reference)),
    )
    invoke_tool(comms, "comms_decision", {
        **request, "chosen": "PUBLIC_LATER_CHOICE", "rejected": ["PUBLIC_ALTERNATIVE"],
        "to": "#team", "change": {"kind": "correction", "original": first["reference"]},
    })
    later = read_history(comms)[-1]
    wire_before = (comms.root / "bus.jsonl").read_bytes()
    captured = {
        name: comms.bus.log.retained_context(name, comms.registry).retained
        for name in ("alpha", "beta", "gamma")
    }
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
