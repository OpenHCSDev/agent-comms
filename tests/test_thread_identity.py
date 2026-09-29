"""S5 identity boundaries: process, turn, provenance and saved-data preservation."""

import json
import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.read_basis import Conversation
from agent_comms.registration import Registration
from agent_comms.thread_identity import GenerationCounter, ThreadIncarnation, TurnIdentity
from agent_comms.thread_status import IdleThreadStatus
from agent_comms.threads import Thread


def registry_with_owner(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    registry.register(Thread("owner", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
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
    assert registry.snapshot().admission_generations == before.admission_generations
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
    assert after.admission_generations["owner"] > before.admission_generations["owner"]


def test_registry_cannot_forge_turn_counter(tmp_path):
    registry = registry_with_owner(tmp_path)
    registry.register(replace(registry.require("owner"), turn_generation=300))
    assert registry.require("owner").turn_generation == 0
    registry.lease_local_turn("owner", "turn")
    registry.release_turn(registry.require("owner").turn_lease)[0]
    registry.register(replace(registry.require("owner"), turn_generation=500))
    assert registry.require("owner").turn_generation == 1


def test_delete_and_rebind_changes_identity_and_keeps_counter_tombstone(tmp_path):
    registry = registry_with_owner(tmp_path)
    before = registry.snapshot().owner_identity("owner")
    registry.unregister("owner")
    registry.remove("owner")
    registry.register(Thread("owner", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
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
    assert registry.snapshot().admission_generations == before.admission_generations


def test_exact_turn_identity_survives_alias_but_not_reused_turn_id(tmp_path):
    comms = Comms(tmp_path)
    comms.threads.register(Thread("owner", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    first = comms.agents.begin_turn("owner", "reused")
    assert first.identity.incarnation == comms.registry.require("owner").incarnation
    comms.registry.rename("owner", "renamed")
    assert comms.agents.finish_turn(first) is not None
    second = comms.agents.begin_turn("renamed", "reused")
    assert second.identity.generation == first.identity.generation + 1
    assert comms.agents.finish_turn(first) is None
    assert comms.agents.finish_turn(second) is not None


@pytest.mark.parametrize("revocation", ["finish", "stop"])
def test_restored_active_turn_has_no_admission_authority(tmp_path, revocation):
    registry = registry_with_owner(tmp_path)
    saved, _ = registry.lease_local_turn("owner", "old")
    if revocation == "stop":
        registry.unregister("owner")
    else:
        registry.release_turn(registry.require("owner").turn_lease)[0]
    registry.register(saved)
    for read in (registry.live_owner_with_generation, registry.live_owner_with_admission):
        with pytest.raises(RelationViolationError, match="unavailable"):
            read("owner")


def test_read_ledger_roundtrip_preserves_historical_identity(tmp_path):
    registry = registry_with_owner(tmp_path)
    owner = registry.require("owner")
    old = {"target": "", "participants": [["owner", owner.created_at]]}
    conversation = Conversation.from_wire(old)
    assert conversation.participants == (owner.incarnation,)
    assert Conversation.from_wire(conversation.to_wire()) == conversation
    registry.lease_local_turn("owner", "new")
    assert conversation.current(registry.snapshot())
    registry.release_turn(registry.require("owner").turn_lease)[0]
    registry.register(registry.require("owner"), new_owner=True)
    assert conversation.current(registry.snapshot())
    registry.unregister("owner")
    registry.remove("owner")
    registry.register(Thread("owner", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())))
    assert not conversation.current(registry.snapshot())


def test_identity_codec_and_counter_domain_are_not_parallel_registries():
    identity = TurnIdentity(ThreadIncarnation("owner", 1.0), 3)
    assert FieldCodec.decode(TurnIdentity, FieldCodec.encode(identity)) == identity
    counter = GenerationCounter()
    assert counter.advance("old") == 1
    counter.rename("old", "new")
    assert counter.generations == {"new": 1}
    assert counter.advance("another") == 2
    with pytest.raises(ValueError):
        FieldCodec.decode(GenerationCounter, {"counter": 1, "generations": {"x": True}})


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
    comms.threads.register(Thread("owner", frozenset(), str(tmp_path)))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    message = comms.messaging.send_message("owner", viewer, "already painted")
    page = comms.views.dm_display_page("owner", worktree=str(tmp_path))
    comms.views.mark_dm_view_read(
        "owner",
        worktree=str(tmp_path),
        through=message.seq,
        expected_display_basis=page.display_basis,
    )
    path = comms.bus.reads.path
    document = json.loads(path.read_text())
    assert all(
        isinstance(participant, list)
        for key in document["messages"]
        for participant in json.loads(key)[2]["participants"]
    )
    reopened = Comms(tmp_path)
    assert message.seq in reopened.bus.reads.seen_sequences(viewer, reopened.registry.snapshot())
    next_message = reopened.messaging.send_message("owner", viewer, "new paint")
    page = reopened.views.dm_display_page("owner", worktree=str(tmp_path))
    reopened.views.mark_dm_view_read(
        "owner",
        worktree=str(tmp_path),
        through=next_message.seq,
        expected_display_basis=page.display_basis,
    )
    assert {message.seq, next_message.seq} <= reopened.bus.reads.seen_sequences(
        viewer, reopened.registry.snapshot()
    )


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
