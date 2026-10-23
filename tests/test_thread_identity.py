"""S5 identity boundaries: process, turn, provenance and saved-data preservation."""

import json
import os
from dataclasses import replace

import pytest

from agent_comms.session_file import SavedSessionFile
from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.comms import Comms
from conftest import installed_comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.read_basis import Conversation
from agent_comms.turn_lease import NoTurnLease
from agent_comms.read_ledger import ReadDocument
from agent_comms.registration import Registration
from agent_comms.thread_identity import (
    ClaimedIdentity,
    GenerationCounter,
    RecordedThreadIncarnation,
    TurnIdentity,
)
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_status import (
    ArchivedThreadStatus,
    ConnectedThreadStatus,
    IdleThreadStatus,
    RunningThreadStatus,
    StoppedThreadStatus,
)
from agent_comms.turn_lease import FinishedTurnFence, NoFinishedTurn
from agent_comms.thread_text import GivenThreadText, NoThreadText
from agent_comms.thread_model import DeclaredModel


def registry_with_owner(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    registry.register(registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    return registry


def test_saved_thread_text_round_trips_null_and_supplied_values(tmp_path):
    comms = Comms(tmp_path)
    for name, title, task in (
        ("untitled", NoThreadText(), NoThreadText()),
        ("named", GivenThreadText("A title"), GivenThreadText("An assignment")),
    ):
        thread = comms.registry.new_thread(
            name, frozenset({"team"}), str(tmp_path), title=title, task=task,
        )
        comms.registry.declare(thread)
        encoded = json.dumps(FieldCodec.encode(thread), sort_keys=True, separators=(",", ":"))
        saved = Comms(tmp_path).registry.require(name)
        assert json.dumps(FieldCodec.encode(saved), sort_keys=True, separators=(",", ":")) == encoded
        assert saved.title == title and saved.task == task
        assert FieldCodec.encode(saved.wake_context(comms.goals))["title"] == FieldCodec.encode(title)
        assert FieldCodec.encode(saved.wake_context(comms.goals))["original_assignment"] == FieldCodec.encode(task)
        assert saved.launch().task == task


def test_metadata_and_turns_do_not_replace_process_owner(tmp_path):
    registry = registry_with_owner(tmp_path)
    before = registry.snapshot()
    owner = before.threads["owner"]
    identity = before.owner_identity("owner")
    registry.register(replace(owner, title=GivenThreadText("new metadata")))
    registry.heartbeat("owner")
    leased, generation = registry.lease_local_turn("owner", "turn")
    assert generation == identity.generation
    assert leased.require_turn_lease().identity == TurnIdentity(identity.incarnation, 1)
    assert registry.snapshot().owner_identity("owner") == identity
    assert registry.snapshot().claim_generation('owner') == before.claim_generation('owner')
    assert registry.release_turn(registry.require("owner").turn_lease)[0]
    finished = registry.require("owner")
    assert finished.turn_generation == 1
    assert finished.turn_lease == NoTurnLease()
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
    assert after.claim_generation("owner") > before.claim_generation("owner")


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
    assert registry.snapshot().claim_generation('owner') == before.claim_generation('owner')


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
    with pytest.raises(RelationViolationError):
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
        wire_key = FieldCodec.record_key(type(scope), "claimed_identity")
        assert wire_key == "admission"
        encoded[wire_key] = FieldCodec.encode(registry_owner)
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
            "lookup", "owner", expected_generation=participant.owner_identity.generation
        ).value
        assert advanced.owner_identity.generation == participant.owner_identity.generation + 1
        assert registry.snapshot().owner_identity("owner") == initial
        registry.register(registry.require("owner"), new_owner=True)
        assert store.participants.get("lookup").owner_identity.generation == advanced.owner_identity.generation


def test_saved_read_ledger_survives_reopen_and_new_ack(tmp_path):
    comms = installed_comms(tmp_path)
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


@pytest.mark.parametrize("withdrawals", [
    pytest.param((Registration.unregister, Registration.unregister, Registration.archive,
                  Registration.begin_delete, Registration.remove), id="stop-archive-delete"),
    pytest.param((Registration.archive, Registration.remove), id="archive-remove"),
    pytest.param((Registration.remove,), id="remove"),
])
@pytest.mark.parametrize(("status", "revokes"), [
    (RunningThreadStatus(), True),
    (IdleThreadStatus(), True),
    (StoppedThreadStatus(), False),
    (ArchivedThreadStatus(), False),
])
def test_withdrawal_revokes_only_an_existing_owner(tmp_path, withdrawals, status, revokes):
    registry = registry_with_owner(tmp_path)
    registry.register(registry.require("owner"), status=status)
    before = registry.snapshot()
    first, *remaining = withdrawals
    first(registry, "owner")
    after = registry.snapshot()
    assert (after.owner_generations["owner"] > before.owner_generations["owner"]) == revokes
    assert after.claim_generation("owner") > before.claim_generation("owner")
    withdrawn = after.owner_generations["owner"]
    for withdraw in remaining:
        withdraw(registry, "owner")
        current = registry.snapshot()
        assert current.owner_generations["owner"] == withdrawn
        assert current.claim_generation("owner") > after.claim_generation("owner")
        after = current


def test_external_unregister_revokes_owner_while_bridge_remains_connected(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    registry.register(registry.new_thread(
        "owner", frozenset(), str(tmp_path), execution=ExternalThreadExecution,
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    before = registry.snapshot()
    assert before.status("owner") == ConnectedThreadStatus()
    registry.unregister("owner")
    after = registry.snapshot()
    assert after.status("owner") == before.status("owner")
    assert after.owner_generations["owner"] > before.owner_generations["owner"]
    assert after.claim_generation("owner") > before.claim_generation("owner")


def test_same_launch_ignores_metadata_and_follows_process_session_and_model(tmp_path, *, thread_registration):
    process = ProcessIdentity.capture(os.getpid())
    thread = thread_registration.new_thread('owner', frozenset(), str(tmp_path), process_identity=process, session_file=SavedSessionFile(str(tmp_path / 'a.jsonl')), model=DeclaredModel('model-a'), created_at=1.0)

    assert thread.same_launch(replace(thread, title=GivenThreadText("Renamed"), tags=frozenset({"team"}), turn_generation=3))
    for changed in (
        replace(thread, process_identity=NoProcess()),
        replace(thread, session_file=SavedSessionFile(str(tmp_path / "b.jsonl"))),
        replace(thread, model=DeclaredModel("model-b")),
        replace(thread, worktree=str(tmp_path / "elsewhere")),
        replace(thread, created_at=2.0),
    ):
        assert not thread.same_launch(changed)


