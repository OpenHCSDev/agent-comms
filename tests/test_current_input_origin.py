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
        facts = inputs.read().compaction_material(comms.registry.require("beta"), None, None)[1]
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
