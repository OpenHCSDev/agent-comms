"""S5 identity boundaries: process, turn, provenance and persisted compatibility."""

import json
import os
from dataclasses import replace

import pytest

from agent_comms.declarations import (
    RelationViolationError,
    Thread,
    ThreadRegistry,
    ThreadStatus,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.operations import Comms
from agent_comms.read_basis import Conversation
from agent_comms.thread_identity import (
    GenerationCounter,
    OwnerIdentity,
    ThreadIncarnation,
    TurnIdentity,
)


def registry_with_owner(tmp_path):
    registry = ThreadRegistry(tmp_path / "registry.json")
    registry.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    return registry


def test_metadata_and_turns_do_not_replace_process_owner(tmp_path):
    registry = registry_with_owner(tmp_path)
    before = registry.snapshot()
    owner = before.threads["owner"]
    identity = before.owner_identity("owner")
    registry.register(replace(owner, title="new metadata"))
    registry.heartbeat("owner")
    claimed, generation = registry.claim_local_turn("owner", "turn")
    assert generation == identity.generation
    assert claimed.turn_identity == TurnIdentity(identity, 1)
    assert registry.snapshot().owner_identity("owner") == identity
    assert registry.snapshot().admission_generations == before.admission_generations
    assert registry.finish_claimed_turn("owner", "turn")
    finished = registry.require("owner")
    assert finished.turn_generation == 1
    assert finished.turn_identity is None
    assert registry.snapshot().owner_identity("owner") == identity
    # A fresh owner consumer can cache this identity without compensating for turns.
    cache = {identity: "owner resource"}
    assert cache[registry.snapshot().owner_identity("owner")] == "owner resource"


def test_owner_replacement_preserves_turn_counter_and_historical_incarnation(tmp_path):
    registry = registry_with_owner(tmp_path)
    registry.claim_local_turn("owner", "turn")
    registry.finish_claimed_turn("owner", "turn")
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
    registry.claim_local_turn("owner", "turn")
    registry.finish_claimed_turn("owner", "turn")
    registry.register(replace(registry.require("owner"), turn_generation=500))
    assert registry.require("owner").turn_generation == 1


def test_delete_and_rebind_changes_identity_and_keeps_counter_tombstone(tmp_path):
    registry = registry_with_owner(tmp_path)
    before = registry.snapshot().owner_identity("owner")
    registry.unregister("owner")
    registry.remove("owner")
    registry.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    after = registry.snapshot().owner_identity("owner")
    assert after.incarnation != before.incarnation
    assert after.generation > before.generation
    assert registry.require("owner").turn_generation == 0


def test_same_process_idle_presence_does_not_rotate_identity(tmp_path):
    registry = registry_with_owner(tmp_path)
    registry.register(registry.require("owner"), status=ThreadStatus.IDLE)
    before = registry.snapshot()
    registry.heartbeat("owner")
    assert registry.snapshot().owner_identity("owner") == before.owner_identity("owner")
    assert registry.snapshot().admission_generations == before.admission_generations


def test_exact_turn_identity_survives_alias_but_not_reused_turn_id(tmp_path):
    comms = Comms(tmp_path)
    comms.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    first = comms.begin_turn("owner", "reused")
    assert first.identity.owner == comms.registry.snapshot().owner_identity("owner")
    comms.registry.rename("owner", "renamed")
    assert comms.finish_turn("owner", "reused", expected=first) is not None
    second = comms.begin_turn("renamed", "reused")
    assert second.identity.generation == first.identity.generation + 1
    assert comms.finish_turn("owner", "reused", expected=first) is None
    assert comms.finish_turn("renamed", "reused", expected=second) is not None


@pytest.mark.parametrize("revocation", ["finish", "stop"])
def test_restored_active_turn_has_no_admission_authority(tmp_path, revocation):
    registry = registry_with_owner(tmp_path)
    saved, _ = registry.claim_local_turn("owner", "old")
    if revocation == "stop":
        registry.unregister("owner")
    else:
        registry.finish_claimed_turn("owner", "old")
    registry.register(saved)
    for read in (registry.live_owner_with_generation, registry.live_owner_with_admission):
        with pytest.raises(RelationViolationError, match="unavailable"):
            read("owner")


def test_legacy_owner_and_turn_metadata_read_then_single_format_write(tmp_path):
    registry = registry_with_owner(tmp_path)
    claimed, generation = registry.claim_local_turn("owner", "old")
    raw = json.loads(registry._path.read_text())
    raw["owner_epochs"] = raw.pop("owner_generations")
    raw["owner_epoch_counter"] = raw.pop("owner_generation_counter")
    raw["turn_epochs"] = {"owner": generation}
    del raw["threads"]["owner"]["active_turn"]["owner_generation"]
    registry._path.write_text(json.dumps(raw))
    reopened = ThreadRegistry(registry._path)
    assert reopened.live_owner_with_generation("owner") == (claimed, generation)
    assert reopened.require("owner").incarnation == claimed.incarnation
    reopened.finish_claimed_turn("owner", "old")
    persisted = json.loads(registry._path.read_text())
    assert not {"owner_epochs", "owner_epoch_counter", "turn_epochs"} & persisted.keys()
    assert persisted["owner_generations"]["owner"] == generation
    assert ThreadRegistry(registry._path).require("owner").turn_generation == 1


def test_old_read_conversation_and_new_codec_have_same_identity(tmp_path):
    registry = registry_with_owner(tmp_path)
    owner = registry.require("owner")
    old = {"target": "", "participants": [["owner", owner.created_at]]}
    conversation = Conversation.from_wire(old)
    assert conversation.participants == (owner.incarnation,)
    assert FieldCodec.decode(Conversation, FieldCodec.encode(conversation)) == conversation
    registry.claim_local_turn("owner", "new")
    assert conversation.current(registry.snapshot())
    registry.finish_claimed_turn("owner", "new")
    registry.register(registry.require("owner"), new_owner=True)
    assert conversation.current(registry.snapshot())
    registry.unregister("owner")
    registry.remove("owner")
    registry.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    assert not conversation.current(registry.snapshot())


def test_identity_codec_and_counter_domain_are_not_parallel_registries():
    identity = TurnIdentity(OwnerIdentity(ThreadIncarnation("owner", 1.0), 2), 3)
    assert FieldCodec.decode(TurnIdentity, FieldCodec.encode(identity)) == identity
    counter = GenerationCounter()
    assert counter.advance("old") == 1
    counter.rename("old", "new")
    assert counter.generations == {"new": 1}
    assert counter.advance("another") == 2
    with pytest.raises(ValueError):
        FieldCodec.decode(GenerationCounter, {"counter": 1, "generations": {"x": True}})
