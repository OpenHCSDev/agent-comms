"""Original authored ingress and neutral historical evidence share one input store."""

import asyncio
from dataclasses import replace

import pytest
from acp.schema import TextContentBlock

from agent_comms.acp_extension import QueuePromptRequest, decode_request, encode_request
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_states import ActiveGoal, CompletedGoal, PausedGoal
from agent_comms.goals import AbsentGoalCheckpoint, Goal, GoalRevision, PresentGoalCheckpoint
from agent_comms.input_attempt import ReservedInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.input_origin import HumanInputOrigin, UnattributedInputOrigin, WireInputOrigin
from agent_comms.message_reference import MessageReference
from agent_comms.queued_input import QueuedInput
from agent_comms.retained_task_facts import CurrentHumanInputTaskFact, HumanInputTaskFact, InputTaskFact
from agent_comms.store_files import _store_lock
from test_acp_queue_contract import _owner


class Client:
    async def session_update(self, **kwargs):
        pass


def capture(comms):
    return HumanInputOrigin.capture(comms, comms.registry.snapshot().admission_identity("beta"))


def test_queue_observation_owns_capture_and_rejects_malformed_available_scope(tmp_path):
    from agent_comms.acp_extension import (
        AvailableQueueProjection, PendingQueueProjection, QueueChangedUpdate,
        QueueScope, UnavailableQueueProjection,
    )
    comms, agent, _, _ = _owner(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    admission = comms.registry.snapshot().admission_identity('beta')
    scope = QueueScope('beta', admission, comms.registry.require('beta').pid)
    for projection in (PendingQueueProjection(), UnavailableQueueProjection()):
        observation = QueueChangedUpdate(None, 0, projection)
        assert FieldCodec.decode(QueueChangedUpdate, FieldCodec.encode(observation)) == observation
        def unavailable_scope():
            raise AssertionError('Unavailable projection acquired input scope')
        with pytest.raises(ValueError, match='queue'):
            observation.projection.capture_human_input(comms, unavailable_scope)
    malformed = FieldCodec.encode(QueueChangedUpdate(scope, 0, AvailableQueueProjection()))
    malformed['scope'] = None
    with pytest.raises(ValueError, match='original attachment scope'):
        FieldCodec.decode(QueueChangedUpdate, malformed)
    observation = QueueChangedUpdate(scope, 0, AvailableQueueProjection())
    assert observation.projection.capture_human_input(comms, lambda: scope) == capture(comms)
    assert not agent.inputs.dispositions.read().rows


async def test_original_human_followup_survives_forwarding_reservation_and_native_disposition(tmp_path):
    comms, agent, _, _ = _owner(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    agent.on_connect(Client())
    comms.agents.begin_turn("beta", "held-native", "Existing original")
    agent.inputs.backend_inboxes["beta"] = asyncio.Queue()
    origin = capture(comms)
    before = comms.bus.log.full_history()
    command = QueuePromptRequest("Never replay UNKNOWN", origin=origin)
    assert decode_request(encode_request(command)) == command
    try:
        await agent.prompt(session_id="beta", prompt=[TextContentBlock(type="text", text=command.user_text)],
                           field_meta=encode_request(command))
        inputs = agent.inputs.dispositions
        key = "acp:" + command.input_id
        row = InputDispositions(inputs.path).read().lookup(key)
        assert row.origin == origin and row.source_text == command.user_text
        assert len(inputs.read().rows) == 1 and comms.bus.log.full_history() == before
        from agent_comms.pi_commands import Prompt
        queued = agent.inputs.backend_inboxes['beta'].get_nowait()
        queued.pop('_input_id')
        native_prompt = Prompt.from_wire(queued)
        assert native_prompt.message == 'User follow-up:\n' + command.user_text
        contribution, = native_prompt.context_contributions
        assert contribution.kind == 'user_followup'
        assert row.context_provenance() in contribution.provenance
        native_id = "b" * 32
        sent = "User follow-up:\n" + command.user_text
        assert inputs.bind(key, admission=row.admission, turn_id="held-native", native_id=native_id, text=sent)
        assert inputs.started(key, turn_id="held-native", native_id=native_id, text=sent)
        # Existing terminal settlement never rewrites a started original as
        # unsent or grants a replay; the canonical native completion owns that.
        assert not inputs.settle_unbound((key,))
        terminal = InputDispositions(inputs.path).read().lookup(key)
        assert terminal.origin == origin
        assert terminal.has_started and terminal.source_text == command.user_text
        facts = inputs.read().compaction_material(comms.registry.require("beta"), (), None)[1]
        assert facts[0].for_owner(comms.registry.require("beta"), comms.registry.snapshot()) == CurrentHumanInputTaskFact(terminal)
    finally:
        await agent.shutdown()


def test_foreign_or_changed_origin_refused_before_any_reservation(tmp_path):
    comms, agent, _, _ = _owner(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    origin = capture(comms)
    invalid = (replace(origin, root_id="f" * 32),
               replace(origin, author=replace(origin.author, created_at=origin.author.created_at + 1)),
               replace(origin, project="/another/project"),
               WireInputOrigin(origin.root_id, MessageReference(1, "forged-reference")))
    for witness in invalid:
        with _store_lock(comms._wire_lock_path), pytest.raises(RelationViolationError):
            QueuedInput.capture(agent.inputs, "beta", text="Original", prompt="Original",
                                echo=True, images=(), controller=Client(), origin=witness)
    with _store_lock(comms._wire_lock_path), pytest.raises(RelationViolationError, match="controller"):
        QueuedInput.capture(agent.inputs, "beta", text="Original", prompt="Original",
                            echo=True, images=(), controller=None, origin=origin)
    assert agent.inputs.dispositions.read().rows == {}


def test_historical_unknown_author_stays_neutral_and_human_scope_invalidates(tmp_path):
    comms, agent, _, _ = _owner(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    origin = capture(comms)
    neutral = ReservedInput("old", None, "beta", origin.admission.admission_generation,
                            "beta", "Exact old user-looking text")
    payload = FieldCodec.encode(neutral)
    assert "origin" not in payload
    restored = FieldCodec.decode(ReservedInput, payload)
    assert restored.origin == UnattributedInputOrigin()
    assert restored.origin.retained_fact(restored) == InputTaskFact(restored)
    with pytest.raises(RelationViolationError, match="author witness"):
        HumanInputTaskFact(restored)
    authored = replace(restored, origin=origin)
    fact = origin.retained_fact(authored)
    owner = comms.registry.require("beta")
    assert fact.for_owner(owner, comms.registry.snapshot()) == CurrentHumanInputTaskFact(authored)
    changed = replace(owner, worktree="/another/project")
    assert fact.for_owner(changed, comms.registry.snapshot()) == HumanInputTaskFact(authored)
    routed = replace(restored, origin=WireInputOrigin(origin.root_id, MessageReference(1, "original-id")))
    assert routed.origin.retained_fact(routed) == InputTaskFact(routed)
    assert FieldCodec.decode(ReservedInput, FieldCodec.encode(routed)) == routed


@pytest.mark.parametrize("state", [ActiveGoal(), PausedGoal(), CompletedGoal()])
def test_original_goal_checkpoint_survives_codec_and_refuses_changed_scope(tmp_path, state):
    comms, agent, _, _ = _owner(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    absent = capture(comms)
    assert absent.goal == AbsentGoalCheckpoint()
    assert FieldCodec.encode(absent)["goal"] == {"kind": "absent"}

    owner = comms.registry.require("beta")
    goal = Goal("Recorded scope", "original-goal", revision=4, state=state)
    comms.registry.register(replace(owner, goal=goal))
    present = capture(comms)
    assert present.goal == PresentGoalCheckpoint(goal.checkpoint)
    assert comms.registry.require("beta").require_goal_checkpoint(goal.checkpoint) == goal
    assert FieldCodec.encode(GoalRevision(goal.id, goal.revision)) == {
        "id": "original-goal", "revision": 4,
    }
    assert FieldCodec.encode(goal)["state"]["kind"] == state.declared_name
    encoded = FieldCodec.encode(present)
    assert encoded["goal"] == {
        "kind": "present", "revision": {"id": "original-goal", "revision": 4},
    }
    restored = FieldCodec.decode(HumanInputOrigin, encoded)
    assert restored == present
    with pytest.raises(ValueError):
        FieldCodec.decode(HumanInputOrigin, {**encoded, "goal": None})

    with _store_lock(comms._wire_lock_path), pytest.raises(RelationViolationError, match="scope"):
        QueuedInput.capture(agent.inputs, "beta", text="Original", prompt="Original",
                            echo=True, images=(), controller=Client(), origin=absent)
    assert agent.inputs.dispositions.read().rows == {}

    with _store_lock(comms._wire_lock_path):
        queued, _ = QueuedInput.capture(agent.inputs, "beta", text="Original", prompt="Original",
                                        echo=True, images=(), controller=Client(), origin=restored)
    assert agent.inputs.dispositions.read().lookup(queued.key).origin == restored
    assert restored.applies(comms.registry.require("beta"), comms.registry.snapshot())
    current = comms.registry.require("beta")
    comms.registry.register(replace(current, goal=replace(goal, revision=5)))
    assert not restored.applies(comms.registry.require("beta"), comms.registry.snapshot())
    comms.registry.register(replace(comms.registry.require("beta"), goal=None))
    assert not restored.applies(comms.registry.require("beta"), comms.registry.snapshot())
    with pytest.raises(ValueError, match="goal changed"):
        comms.registry.require("beta").require_goal_checkpoint(goal.checkpoint)


def test_direct_human_input_pin_shares_original_source_lineage_without_replay(tmp_path):
    from agent_comms.task_sources import CorrectionTaskChange, UserTaskDrop, UserTaskSupersession
    from agent_comms.retained_task_facts import RetainedTaskFacts
    from agent_comms.threads import Thread
    from agent_comms.turn_context import WireProvenance

    comms, agent, _, _ = _owner(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    origin = capture(comms)
    exact = 'Keep the original λ /source.\nNever replay UNKNOWN.'
    with _store_lock(comms._wire_lock_path):
        queued, _ = QueuedInput.capture(agent.inputs, 'beta', text=exact, prompt=exact,
                                        echo=True, images=(), controller=Client(), origin=origin)
    inputs = agent.inputs.dispositions
    row = inputs.read().lookup(queued.key)
    original_inputs = inputs.path.read_bytes()
    subject = row.context_provenance()
    pin = comms.messaging.pin_input_constraint('beta', subject, worktree=origin.project)
    assert pin.notice and not pin.starts_turn and exact not in pin.body
    assert 'source_turn' not in FieldCodec.encode(pin.task)
    assert FieldCodec.decode(type(pin), FieldCodec.encode(pin)) == pin

    def captured(name):
        owner = comms.registry.require(name)
        with comms.bus.log.certified_read() as source:
            wire_facts = tuple(source.retained_task_facts(owner.incarnation))
        input_facts = tuple(r.origin.retained_fact(r) for r in inputs.read().rows.values())
        return RetainedTaskFacts(wire_facts + input_facts).for_owner(owner, comms.registry.snapshot())

    snapshot = comms.registry.snapshot()
    retained = captured('beta')
    assert retained.current_authored_sources(snapshot.require('beta'), snapshot) == (pin,)
    assert retained.original_text_source(pin) == row
    assert FieldCodec.decode(RetainedTaskFacts, FieldCodec.encode(retained)) == retained
    with comms.bus.log.certified_read() as source:
        wire_only = tuple(source.retained_task_facts(snapshot.require('beta').incarnation))
    with pytest.raises(RelationViolationError, match='original captured wording'):
        RetainedTaskFacts(wire_only).for_owner(snapshot.require('beta'), snapshot)
    repeated = comms.messaging.pin_input_constraint('beta', subject, worktree=origin.project)
    assert captured('beta').current_authored_sources(snapshot.require('beta'), snapshot) == (repeated,)
    before = (comms.root / 'bus.jsonl').read_bytes()
    comms.registry.declare(Thread('unaddressed', frozenset(), origin.project))
    with pytest.raises(RelationViolationError, match='did not receive'):
        comms.messaging.pin_input_constraint('unaddressed', subject, worktree=origin.project)
    with pytest.raises(RelationViolationError, match='provenance'):
        comms.messaging.pin_input_constraint('beta', replace(subject, key='absent'), worktree=origin.project)
    with pytest.raises(RelationViolationError, match='provenance'):
        comms.messaging.pin_input_constraint('beta', replace(subject, origin=replace(origin, root_id='f'*32)),
                                             worktree=origin.project)
    with pytest.raises(RelationViolationError, match='original human input'):
        comms.messaging.pin_input_constraint('beta', WireProvenance(MessageReference(1, 'not-input')),
                                             worktree=origin.project)
    assert (comms.root / 'bus.jsonl').read_bytes() == before
    assert inputs.path.read_bytes() == original_inputs

    # Same text, separately reserved original inputs never acquire one identity.
    with _store_lock(comms._wire_lock_path):
        second, _ = QueuedInput.capture(agent.inputs, 'beta', text=exact, prompt=exact,
                                        echo=True, images=(), controller=Client(), origin=origin)
    second_subject = inputs.read().lookup(second.key).context_provenance()
    second_pin = comms.messaging.pin_input_constraint('beta', second_subject, worktree=origin.project)
    assert second_subject != subject
    assert captured('beta').current_authored_sources(snapshot.require('beta'), snapshot) == (repeated, second_pin)
    comms.registry.rename('beta', 'renamed-beta')
    owner = comms.registry.require('renamed-beta')
    assert row.matches_owner(owner.incarnation)
    assert not row.matches_owner(replace(owner.incarnation, created_at=owner.created_at + 1))
    assert captured(owner.name).original_text_source(second_pin) == inputs.read().lookup(second.key)
    revised = comms.messaging.send_user_message(owner.name, 'Exact human correction.', worktree=origin.project,
        task=UserTaskSupersession(CorrectionTaskChange(repeated.reference)))
    current = captured(owner.name)
    assert current.current_authored_sources(owner, comms.registry.snapshot()) == (revised, second_pin)
    comms.messaging.send_user_message(owner.name, 'Drop the second original.', worktree=origin.project,
        task=UserTaskDrop(CorrectionTaskChange(second_pin.reference)))
    assert captured(owner.name).current_authored_sources(owner, comms.registry.snapshot()) == (revised,)
    assert inputs.read().lookup(row.key) == row
    assert inputs.read().lookup(second.key).unresolved
