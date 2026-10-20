"""S5 identity boundaries: process, turn, provenance and saved-data preservation."""

import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_errors import ParticipantNotRegistered
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.messages import InfoKind, Message
from agent_comms.read_basis import Conversation
from agent_comms.read_ledger import ReadDocument
from agent_comms.registration import Registration
from agent_comms.response_policy import CollectivePolicy, InformationalPolicy
from agent_comms.thread_identity import (
    AgentThreadRole,
    ClaimedIdentity,
    GenerationCounter,
    RecordedThreadIncarnation,
    ThreadRole,
    TurnIdentity,
    UserThreadRole,
)
from agent_comms.thread_status import IdleThreadStatus
from agent_comms.turn_lease import FinishedTurnFence, NoFinishedTurn


def registry_with_owner(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    registry.register(registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    return registry


def test_metadata_and_turns_do_not_replace_process_owner(tmp_path):
    registry = registry_with_owner(tmp_path)
    before = registry.snapshot()
    owner = before.threads["owner"]
    identity = before.owner_identity("owner")
    registry.register(replace(owner, title="new metadata"))
    registry.heartbeat("owner")
    leased, generation = registry.lease_local_turn("owner", "turn")
    assert generation == identity.generation
    assert leased.turn_identity == TurnIdentity(identity.incarnation, 1)
    assert registry.snapshot().owner_identity("owner") == identity
    assert registry.snapshot().claimed_generations == before.claimed_generations
    assert registry.release_turn(registry.require("owner").turn_lease)[0]
    finished = registry.require("owner")
    assert finished.turn_generation == 1
    assert finished.turn_identity is None
    assert registry.snapshot().owner_identity("owner") == identity
    # A fresh owner consumer can cache this identity without compensating for turns.
    cache = {identity: "owner resource"}
    assert cache[registry.snapshot().owner_identity("owner")] == "owner resource"


def test_owner_replacement_preserves_turn_counter_and_historical_incarnation(tmp_path):
    registry = registry_with_owner(tmp_path)
    registry.lease_local_turn("owner", "turn")
    registry.release_turn(registry.require("owner").turn_lease)[0]
    before = registry.snapshot()
    registry.register(before.threads["owner"], new_owner=True)
    after = registry.snapshot()
    assert before.threads["owner"].incarnation == after.threads["owner"].incarnation
    assert before.owner_identity("owner") != after.owner_identity("owner")
    assert after.threads["owner"].turn_generation == 1
    assert after.claimed_generations["owner"] > before.claimed_generations["owner"]


def test_registry_cannot_forge_turn_counter(tmp_path):
    registry = registry_with_owner(tmp_path)
    registry.register(replace(registry.require("owner"), turn_generation=300))
    assert registry.require("owner").turn_generation == 0
    registry.lease_local_turn("owner", "turn")
    registry.release_turn(registry.require("owner").turn_lease)[0]
    registry.register(replace(registry.require("owner"), turn_generation=500))
    assert registry.require("owner").turn_generation == 1


def test_delete_and_rebind_changes_identity_and_keeps_counter_tombstone(tmp_path, *, thread_registration):
    registry = registry_with_owner(tmp_path)
    before = registry.snapshot().owner_identity("owner")
    registry.unregister("owner")
    registry.remove("owner")
    registry.register(thread_registration.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    after = registry.snapshot().owner_identity("owner")
    assert after.incarnation != before.incarnation
    assert after.generation > before.generation
    assert registry.require("owner").turn_generation == 0


def test_same_process_idle_presence_does_not_rotate_identity(tmp_path):
    registry = registry_with_owner(tmp_path)
    registry.register(registry.require("owner"), status=IdleThreadStatus())
    before = registry.snapshot()
    registry.heartbeat("owner")
    assert registry.snapshot().owner_identity("owner") == before.owner_identity("owner")
    assert registry.snapshot().claimed_generations == before.claimed_generations


def test_exact_turn_identity_survives_alias_but_not_reused_turn_id(tmp_path):
    comms = Comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    first = comms.agents.begin_turn("owner", "reused").turn_lease
    assert first.identity.incarnation == comms.registry.require("owner").incarnation
    comms.registry.rename("owner", "renamed")
    assert isinstance(comms.agents.finish_turn(first), FinishedTurnFence)
    second = comms.agents.begin_turn("renamed", "reused").turn_lease
    assert second.identity.generation == first.identity.generation + 1
    assert isinstance(comms.agents.finish_turn(first), NoFinishedTurn)
    assert isinstance(comms.agents.finish_turn(second), FinishedTurnFence)


@pytest.mark.parametrize("revocation", ["finish", "stop"])
def test_restored_active_turn_has_no_claim_authority(tmp_path, revocation):
    registry = registry_with_owner(tmp_path)
    saved, _ = registry.lease_local_turn("owner", "old")
    if revocation == "stop":
        registry.unregister("owner")
    else:
        registry.release_turn(registry.require("owner").turn_lease)[0]
    registry.register(saved)
    with pytest.raises(RelationViolationError, match="live owner turn claim_scope is no longer current"):
        registry.live_owner_with_claim("owner")


def test_read_ledger_roundtrip_preserves_historical_identity(tmp_path, *, thread_registration):
    registry = registry_with_owner(tmp_path)
    owner = registry.require("owner")
    original = {"kind": "dm", "participants": [FieldCodec.encode(owner.incarnation)]}
    conversation = FieldCodec.decode(Conversation, original)
    assert conversation.participants == (owner.incarnation,)
    assert FieldCodec.decode(Conversation, FieldCodec.encode(conversation)) == conversation
    registry.lease_local_turn("owner", "new")
    assert conversation.current(registry.snapshot())
    registry.release_turn(registry.require("owner").turn_lease)[0]
    registry.register(registry.require("owner"), new_owner=True)
    assert conversation.current(registry.snapshot())
    registry.unregister("owner")
    registry.remove("owner")
    registry.register(thread_registration.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    assert not conversation.current(registry.snapshot())


def test_identity_codec_and_counter_domain_are_not_parallel_registries():
    identity = TurnIdentity(RecordedThreadIncarnation("owner", 1.0), 3)
    assert FieldCodec.decode(TurnIdentity, FieldCodec.encode(identity)) == identity
    counter = GenerationCounter()
    assert counter.advance("old") == 1
    counter.rename("old", "new")
    assert counter.generations == {"new": 1}
    assert counter.advance("another") == 2
    with pytest.raises(ValueError):
        FieldCodec.decode(GenerationCounter, {"counter": 1, "generations": {"x": True}})


def test_claim_scope_cannot_decode_a_registry_process_owner(tmp_path):
    from agent_comms.acp_extension import LiveCursorScope, LiveQueueScope
    from agent_comms.queued_input import QueuedInputContext

    registry = registry_with_owner(tmp_path)
    snapshot = registry.snapshot()
    owner = snapshot.threads["owner"]
    claim_scope = snapshot.claimed_identity("owner")
    registry_owner = snapshot.owner_identity("owner")
    assert claim_scope.claimed_generation == registry_owner.generation
    assert claim_scope != registry_owner
    context = QueuedInputContext.capture(owner, claim_scope)
    assert context.owns(claim_scope)
    assert not context.owns(registry_owner)
    # Repeated revocation advances claim_scope without a second owner replacement.
    registry.unregister("owner")
    registry.unregister("owner")
    registry.heartbeat("owner")
    snapshot = registry.snapshot()
    owner = snapshot.threads["owner"]
    claim_scope = snapshot.claimed_identity("owner")
    registry_owner = snapshot.owner_identity("owner")
    assert claim_scope.claimed_generation != registry_owner.generation
    original_binding = snapshot.owner_binding("owner")
    assert not original_binding.superseded_by(snapshot.owner_binding("owner"))
    for scope in (LiveQueueScope("official-session", claim_scope, owner.require_process().pid),
                  LiveCursorScope("official-session", "a" * 32, claim_scope, owner.require_process().pid)):
        encoded = FieldCodec.encode(scope)
        assert FieldCodec.decode(type(scope), encoded) == scope
        encoded["claim_scope"] = FieldCodec.encode(registry_owner)
        with pytest.raises(ValueError):
            FieldCodec.decode(type(scope), encoded)
    assert FieldCodec.decode(ClaimedIdentity, FieldCodec.encode(claim_scope)) == claim_scope


def test_coordination_assignment_generation_is_independent_of_registry_process(tmp_path):
    from agent_comms.coordinator import Coordination

    registry = registry_with_owner(tmp_path)
    initial = registry.snapshot().owner_identity("owner")
    with Coordination(str(tmp_path / "coordination.sqlite3")) as store:
        participant = store.participants.register("lookup", "Owner", "owner", committed=True).value
        advanced = store.participants.advance_generation(
            "lookup", "owner", expected_generation=participant.participant_generation
        ).value
        assert advanced.participant_generation == participant.participant_generation + 1
        assert registry.snapshot().owner_identity("owner") == initial
        registry.register(registry.require("owner"), new_owner=True)
        assert store.participants.get("lookup").participant_generation == advanced.participant_generation


def test_saved_read_ledger_survives_reopen_and_new_ack(tmp_path):
    comms = Comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    message = comms.messaging.send("owner", viewer, "already painted").message
    page = comms.views.dm_display_page("owner", worktree=str(tmp_path))
    comms.views.mark_page_read(page, worktree=str(tmp_path))
    path = comms.bus.reads.path
    document = FieldCodec.decode(ReadDocument, json.loads(path.read_text()))
    viewer_identity = comms.registry.require(viewer).incarnation
    assert tuple(document.messages) == (viewer_identity,)
    assert {participant for conversation in document.messages[viewer_identity].conversations
            for participant in conversation.participants} == {
                viewer_identity, comms.registry.require("owner").incarnation}
    reopened = Comms(tmp_path)
    snapshot = reopened.registry.snapshot()
    conversation = reopened.bus.reads.conversation(message, snapshot)
    assert message.seq in reopened.bus.reads.cursor(viewer, snapshot).for_conversation(conversation)
    next_message = reopened.messaging.send("owner", viewer, "new paint").message
    page = reopened.views.dm_display_page("owner", worktree=str(tmp_path))
    reopened.views.mark_page_read(page, worktree=str(tmp_path))
    seen = reopened.bus.reads.cursor(viewer, reopened.registry.snapshot()).for_conversation(conversation)
    assert all(sequence in seen for sequence in (message.seq, next_message.seq))


def test_stopped_status_changes_do_not_invent_another_owner(tmp_path):
    registry = registry_with_owner(tmp_path)
    registry.unregister("owner")
    stopped = registry.snapshot().owner_identity("owner")
    registry.unregister("owner")
    registry.archive("owner")
    registry.begin_delete("owner")
    assert registry.snapshot().owner_identity("owner") == stopped
    registry.remove("owner")
    assert registry.snapshot().owner_generations["owner"] == stopped.generation


def test_same_launch_ignores_metadata_and_follows_process_session_and_model(tmp_path, *, thread_registration):
    process = ProcessIdentity.capture(os.getpid())
    thread = thread_registration.new_thread('owner', frozenset(), str(tmp_path), process_identity=process, session_file=str(tmp_path / 'a.jsonl'), model='model-a', created_at=1.0)

    assert thread.same_launch(replace(thread, title="Renamed", tags=frozenset({"team"}), turn_generation=3))
    for changed in (
        replace(thread, process_identity=NoProcess()),
        replace(thread, session_file=str(tmp_path / "b.jsonl")),
        replace(thread, model="model-b"),
        replace(thread, worktree=str(tmp_path / "elsewhere")),
        replace(thread, created_at=2.0),
    ):
        assert not thread.same_launch(changed)


@pytest.mark.parametrize(
    ("role", "wire", "delivers", "registers", "executes", "human", "policy", "reply", "input_at", "origin"),
    [
        (AgentThreadRole, "agent", True, True, True, False, InformationalPolicy, None, 5.0, "external agent"),
        (UserThreadRole, "user", False, False, False, True, CollectivePolicy, "sender", 9.0, "human"),
    ],
)
def test_each_thread_role_member_owns_its_role_behaviour(
    tmp_path, role, wire, delivers, registers, executes, human, policy, reply, input_at, origin
):
    """One declaration per member pins every method the family overrides and its wire spelling."""
    message = Message("sender", "peer", "hello", InfoKind, sender_role=role)
    assert (FieldCodec.encode(role), FieldCodec.decode(type[ThreadRole], wire)) == (wire, role)

    delivered, targets = role.initial_delivery(message, "peer")
    assert (targets == ["peer"], delivered.role == message.role) == (delivers, delivers)

    with Coordination(str(tmp_path / "coordination.sqlite3")) as store:
        role.register_sender(store.participants, "lookup-sender", "sender")
        try:
            store.participants.get("lookup-sender")
            registered = True
        except ParticipantNotRegistered:
            registered = False
    assert registered is registers

    refusal = RelationViolationError("refused")
    for require, admits in ((role.require_executable, executes), (role.require_user, human)):
        if admits:
            require(refusal)
        else:
            with pytest.raises(RelationViolationError, match="refused"):
                require(refusal)

    item = object()
    assert role.executor(item) == ((item,) if executes else ())
    assert role.human(item) == ((item,) if human else ())
    assert role.page_human_sender() is human

    barrier = MaintenanceBarrier(tmp_path / "wire" / "registry.json")
    role.require_maintenance_open(barrier, True)
    barrier.begin(MaintenanceOperator.current())
    role.require_maintenance_open(barrier, False)
    if executes:
        with pytest.raises(RelationViolationError, match="under maintenance"):
            role.require_maintenance_open(barrier, True)
    else:
        role.require_maintenance_open(barrier, True)

    class Reads:
        def displayed_incarnation(self, *shown, document):
            return (shown, document)

    shown = role.displayed_recipient_effect(
        Reads(), message, SimpleNamespace(incarnation="thread"), "snapshot", "document")
    assert shown == (((message, "thread", "snapshot"), "document") if human else ())
    assert role.channel_policy() is policy.instance()
    assert role.reply_target(message) == reply
    assert role.last_user_input(5.0, 9.0) == input_at
    assert tuple(role.user_task_facts(message)) == (
        tuple(message.task.user_task_facts(message)) if human else ()
    )
    assert role.origin_label == origin
