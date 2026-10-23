"""Original authored ingress and neutral historical evidence share one input store."""

from agent_comms.lifecycle_capability import Unresolved
from agent_comms.messages import Message, NoticeRole
from contextlib import ExitStack

from dataclasses import replace

import pytest
from acp.schema import TextContentBlock

from agent_comms.acp_extension import QueuePromptRequest, decode_request, encode_request
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_states import ActiveGoal, CompletedGoal, PausedGoal
from agent_comms.goals import AbsentGoalCheckpoint, Goal, GoalRevision, NoGoal, PresentGoalCheckpoint
from agent_comms.input_attempt import ReservedInput
from agent_comms.input_origin import HumanInputOrigin, UnattributedInputOrigin, WireInputOrigin
from agent_comms.message_reference import MessageReference
from agent_comms.queued_input import QueuedInput
from agent_comms.retained_task_facts import CurrentHumanInputTaskFact, HumanInputTaskFact, InputTaskFact
from test_acp_queue_contract import _owner
from stored_inputs import stored_inputs


class Client:
    async def session_update(self, **kwargs):
        pass


@pytest.fixture
async def live_owner(tmp_path):
    comms, agent, _, _ = _owner(tmp_path)
    try:
        yield comms, agent
    finally:
        await agent.shutdown()


def capture(comms):
    return HumanInputOrigin.capture(comms, comms.registry.snapshot().claimed_identity("beta"))


async def test_queue_observation_owns_capture_and_rejects_malformed_available_scope(live_owner):
    from agent_comms.acp_extension import (
        AvailableQueueProjection, PendingQueueProjection, QueueChangedUpdate,
        LiveQueueScope, NoQueueScope, UnavailableQueueProjection,
    )
    comms, agent = live_owner
    scope = await agent.inputs.queue_binding('beta')
    assert scope == LiveQueueScope(
        'beta', comms.registry.snapshot().claimed_identity('beta'),
        comms.registry.require('beta').require_process(),
    )
    for projection in (PendingQueueProjection(), UnavailableQueueProjection()):
        observation = QueueChangedUpdate(NoQueueScope(), 0, projection)
        assert FieldCodec.decode(QueueChangedUpdate, FieldCodec.encode(observation)) == observation
        def unavailable_scope():
            raise AssertionError('Unavailable projection acquired input scope')
        with pytest.raises(ValueError):
            observation.projection.capture_human_input(comms, unavailable_scope)
    malformed = FieldCodec.encode(QueueChangedUpdate(scope, 0, AvailableQueueProjection()))
    malformed['scope'] = None
    with pytest.raises(ValueError):
        FieldCodec.decode(QueueChangedUpdate, malformed)
    observation = QueueChangedUpdate(scope, 0, AvailableQueueProjection())
    assert observation.projection.capture_human_input(comms, lambda: scope) == capture(comms)
    assert not stored_inputs(agent.inputs.dispositions.root).rows


async def test_original_human_followup_survives_forwarding_reservation_and_native_disposition(live_owner):
    comms, agent = live_owner
    agent.on_connect(Client())
    comms.agents.begin_turn("beta", "held-native", "Existing original")
    agent.inputs.state('beta').open_inbox()
    origin = capture(comms)
    before = comms.bus.bus.revision()
    command = QueuePromptRequest("Never replay UNKNOWN", origin=origin)
    assert decode_request(encode_request(command)) == command
    await agent.prompt(session_id="beta", prompt=[TextContentBlock(type="text", text=command.user_text)],
                       field_meta=encode_request(command))
    inputs = agent.inputs.dispositions
    key = "acp:" + command.input_id
    row = stored_inputs(inputs.root).lookup(key)
    assert row.origin == origin and row.source_text == command.user_text
    assert len(stored_inputs(inputs.root).rows) == 1 and comms.bus.bus.revision() == before
    from agent_comms.turn_context import UserFollowupSegment
    queued = agent.inputs.state('beta').require_inbox().get_nowait()
    assert queued.input_id.value == command.input_id
    assert queued.content.text == 'User follow-up:\n' + command.user_text
    contribution, = queued.content.contributions
    assert contribution.kind is UserFollowupSegment
    assert row.context_provenance() in contribution.provenance
    native_id = "b" * 32
    sent = "User follow-up:\n" + command.user_text
    assert inputs.bind(key, claim_scope=row.claim_generation, turn_id="held-native", native_id=native_id, text=sent)
    assert inputs.started(key, turn_id="held-native", native_id=native_id, text=sent)
    # Existing terminal settlement never rewrites a started original as
    # unsent or grants a replay; the native completion owns that.
    assert inputs.settle_unbound((key,)).lookup(key).has_started
    terminal = stored_inputs(inputs.root).lookup(key)
    assert terminal.origin == origin
    assert terminal.has_started and terminal.source_text == command.user_text
    assert terminal.origin.retained_fact(terminal).for_owner(
        comms.registry.require("beta"), comms.registry.snapshot()
    ) == CurrentHumanInputTaskFact(terminal)


async def test_foreign_or_changed_origin_refused_before_any_reservation(live_owner):
    comms, agent = live_owner
    origin = capture(comms)
    invalid = (replace(origin, root_id="f" * 32),
               replace(origin, author=replace(origin.author, created_at=origin.author.created_at + 1)),
               replace(origin, project="/another/project"),
               WireInputOrigin(origin.root_id, MessageReference(1, "forged-reference")))
    for witness in invalid:
        with comms.registry.store.locked(), pytest.raises(RelationViolationError):
            with ExitStack() as ownership:
                QueuedInput.capture(agent.inputs, "beta", text="Original", prompt="Original",
                                    echo=True, images=(), controller=Client(), origin=witness, ownership=ownership)
                ownership.pop_all()
    with comms.registry.store.locked(), pytest.raises(RelationViolationError):
        with ExitStack() as ownership:
            QueuedInput.capture(agent.inputs, "beta", text="Original", prompt="Original",
                                echo=True, images=(), controller=None, origin=origin, ownership=ownership)
            ownership.pop_all()
    assert stored_inputs(agent.inputs.dispositions.root).rows == {}


async def test_historical_unknown_author_stays_neutral_and_human_scope_invalidates(live_owner):
    comms, agent = live_owner
    origin = capture(comms)
    neutral = ReservedInput("old", None, "beta", origin.claimed_identity.claimed_generation,
                            "beta", "Exact old user-looking text")
    payload = FieldCodec.encode(neutral)
    assert "origin" not in payload
    restored = FieldCodec.decode(ReservedInput, payload)
    assert restored.origin == UnattributedInputOrigin()
    assert restored.origin.retained_fact(restored) == InputTaskFact(restored)
    with pytest.raises(RelationViolationError):
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
async def test_original_goal_checkpoint_survives_codec_and_refuses_changed_scope(live_owner, state):
    comms, agent = live_owner
    absent = capture(comms)
    assert absent.goal == AbsentGoalCheckpoint()
    assert FieldCodec.encode(absent)["goal"] == {"kind": "absent"}

    owner = comms.registry.require("beta")
    goal = Goal("Recorded scope", "original-goal", published_seq=0, revision=4, state=state)
    comms.registry.register(replace(owner, goal=goal))
    present = capture(comms)
    assert present.goal == PresentGoalCheckpoint(goal.revision_key)
    assert comms.registry.require("beta").goal.require_checkpoint(goal.revision_key) == goal
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

    with comms.registry.store.locked(), pytest.raises(RelationViolationError):
        with ExitStack() as ownership:
            QueuedInput.capture(agent.inputs, "beta", text="Original", prompt="Original",
                                echo=True, images=(), controller=Client(), origin=absent, ownership=ownership)
            ownership.pop_all()
    assert stored_inputs(agent.inputs.dispositions.root).rows == {}

    async with QueuedInput.reserve(
        agent.inputs, "beta", "beta", text="Original", prompt="Original",
        echo=True, images=(), controller=Client(), origin=restored,
    ) as reservation:
        queued = reservation.item
        reservation.release()
    assert stored_inputs(agent.inputs.dispositions.root).lookup(queued.key).origin == restored
    assert restored.applies(comms.registry.require("beta"), comms.registry.snapshot())
    current = comms.registry.require("beta")
    comms.registry.register(replace(current, goal=replace(goal, revision=5)))
    assert not restored.applies(comms.registry.require("beta"), comms.registry.snapshot())
    comms.registry.register(replace(comms.registry.require("beta"), goal=NoGoal()))
    assert not restored.applies(comms.registry.require("beta"), comms.registry.snapshot())
    with pytest.raises(ValueError):
        comms.registry.require("beta").goal.require_checkpoint(goal.revision_key)


async def test_direct_human_input_pin_shares_original_source_lineage_without_replay(live_owner):
    from agent_comms.task_sources import CorrectionTaskChange, UserTaskDrop, UserTaskSupersession
    from agent_comms.retained_task_facts import RetainedTaskFacts
    from agent_comms.turn_context import WireProvenance

    comms, agent = live_owner
    origin = capture(comms)
    exact = 'Keep the original λ /source.\nNever replay UNKNOWN.'
    async with QueuedInput.reserve(
        agent.inputs, "beta", "beta", text=exact, prompt=exact,
        echo=True, images=(), controller=Client(), origin=origin,
    ) as reservation:
        queued = reservation.item
        reservation.release()
    inputs = agent.inputs.dispositions
    row = stored_inputs(inputs.root).lookup(queued.key)
    original_inputs = inputs.path.read_bytes()
    subject = row.context_provenance()
    pin = comms.messaging.pin_input_constraint('beta', subject, worktree=origin.project)
    assert isinstance(pin.role, NoticeRole) and not pin.role.response_policy(pin).starts_turn and exact not in pin.body
    assert 'source_turn' not in FieldCodec.encode(pin.task)
    assert FieldCodec.decode(type(pin), FieldCodec.encode(pin)) == pin

    def captured(name):
        owner = comms.registry.require(name)
        with comms.bus.bus.reading() as source:
            wire_facts = RetainedTaskFacts.capture(source, owner.incarnation).facts
        input_facts = tuple(r.origin.retained_fact(r) for r in stored_inputs(inputs.root).rows.values())
        return RetainedTaskFacts(wire_facts + input_facts).for_owner(owner, comms.registry.snapshot())

    snapshot = comms.registry.snapshot()
    retained = captured('beta')
    assert retained.current_authored_sources(snapshot.require('beta'), snapshot) == (Message.from_message(pin),)
    assert retained.original_text_source(pin.reference) == row
    assert FieldCodec.decode(RetainedTaskFacts, FieldCodec.encode(retained)) == retained
    with comms.bus.bus.reading() as source:
        wire_only = RetainedTaskFacts.capture(source, snapshot.require('beta').incarnation).facts
    with pytest.raises(RelationViolationError):
        RetainedTaskFacts(wire_only).for_owner(snapshot.require('beta'), snapshot)
    repeated = comms.messaging.pin_input_constraint('beta', subject, worktree=origin.project)
    assert captured('beta').current_authored_sources(snapshot.require('beta'), snapshot) == (Message.from_message(repeated),)
    before = comms.bus.bus.revision()
    comms.registry.declare(comms.registry.new_thread('unaddressed', frozenset(), origin.project))
    with pytest.raises(RelationViolationError):
        comms.messaging.pin_input_constraint('unaddressed', subject, worktree=origin.project)
    with pytest.raises(RelationViolationError):
        comms.messaging.pin_input_constraint('beta', replace(subject, key='absent'), worktree=origin.project)
    with pytest.raises(RelationViolationError):
        comms.messaging.pin_input_constraint('beta', replace(subject, origin=replace(origin, root_id='f'*32)),
                                             worktree=origin.project)
    with pytest.raises(RelationViolationError):
        comms.messaging.pin_input_constraint('beta', WireProvenance(MessageReference(1, 'not-input')),
                                             worktree=origin.project)
    assert comms.bus.bus.revision() == before
    assert inputs.path.read_bytes() == original_inputs

    # Same text, separately reserved original inputs never acquire one identity.
    async with QueuedInput.reserve(
        agent.inputs, "beta", "beta", text=exact, prompt=exact,
        echo=True, images=(), controller=Client(), origin=origin,
    ) as reservation:
        second = reservation.item
        reservation.release()
    second_subject = stored_inputs(inputs.root).lookup(second.key).context_provenance()
    second_pin = comms.messaging.pin_input_constraint('beta', second_subject, worktree=origin.project)
    assert second_subject != subject
    assert captured('beta').current_authored_sources(snapshot.require('beta'), snapshot) == (
        Message.from_message(repeated), Message.from_message(second_pin),
    )
    comms.registry.rename('beta', 'renamed-beta')
    owner = comms.registry.require('renamed-beta')
    assert row.matches_owner(owner.incarnation)
    assert not row.matches_owner(replace(owner.incarnation, created_at=owner.created_at + 1))
    assert captured(owner.name).original_text_source(second_pin.reference) == stored_inputs(inputs.root).lookup(second.key)
    revised = comms.messaging.send_user_message(owner.name, 'Exact human correction.', worktree=origin.project,
        task=UserTaskSupersession(CorrectionTaskChange(repeated.reference)))
    current = captured(owner.name)
    assert current.current_authored_sources(owner, comms.registry.snapshot()) == (
        Message.from_message(revised), Message.from_message(second_pin),
    )
    comms.messaging.send_user_message(owner.name, 'Drop the second original.', worktree=origin.project,
        task=UserTaskDrop(CorrectionTaskChange(second_pin.reference)))
    assert captured(owner.name).current_authored_sources(owner, comms.registry.snapshot()) == (Message.from_message(revised),)
    assert stored_inputs(inputs.root).lookup(row.key) == row
    stored_inputs(inputs.root).lookup(second.key).require(Unresolved, ValueError("review required"))
