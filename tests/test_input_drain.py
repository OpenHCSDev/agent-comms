"""Owned queue receipts and the real compaction source boundary, without providers."""

from __future__ import annotations

import sys
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest

from agent_comms.acp import CommsAgent
from agent_comms import agent_events as events
from agent_comms.coordinator import Coordination
from agent_comms.acp_extension import (
    InputDeliveryChangedUpdate,
    InputFailedUpdate,
    QueuePromptRequest,
    SendNowRequest,
    SteerPromptRequest,
    decode_updates,
    encode_request,
)
from agent_comms.child_process import ParentedProcess
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.errors import RelationViolationError
from agent_comms.input_disposition import InputDispositions
from agent_comms.owner_compaction_commit import OwnerCompactionCommit
from test_backend_native_lifecycle import native_backend


@pytest.fixture
async def owner(native_backend):
    native = native_backend
    witness = await native.author_history()
    saved = native.session.read_bytes()
    async with native.open_owner() as (agent, session_id):
        async with native.original_input(agent, session_id, "original") as turn:
            launch = agent.turns.persistent_backends[session_id].custody.idle().child.key[0]
            async with OwnerCompactionCommit.open(
                agent._comms.registry.store.path, Path(launch.package), str(native.session),
                future_queue=agent.inputs, native_launch=launch,
            ) as bridge:
                source = await Coordination.run_worker(partial(
                    bridge.capture_source, turn.thread, turn.registry_owner.admission_generation,
                    witness, pending_input_keys=turn.original_keys,
                ))
                yield agent, turn, bridge, witness, source
    assert native.session.read_bytes() == saved
    assert native.provider.posts == 0 and native.starts == []
    assert all(not child.alive() and not child.platform.group_members(child.identity)
               for child in native.children)


async def capture(fixture):
    agent, turn, bridge, witness, source = fixture
    return await Coordination.run_worker(partial(
        bridge.capture_source, turn.thread, turn.registry_owner.admission_generation,
        witness, pending_input_keys=turn.original_keys,
    ))


async def queue(agent, session_id, text="future", delivery=QueuePromptRequest):
    response = await agent.prompt(
        session_id,
        [{"type": "text", "text": text}],
        _meta=encode_request(delivery(defer_display=True)),
    )
    (receipt,) = decode_updates(response.field_meta)
    assert isinstance(receipt, InputDeliveryChangedUpdate)
    key = "acp:" + receipt.input_id
    assert (
        not InputDispositions(agent._comms.root / InputDispositions.filename)
        .read()
        .lookup(key)
        .has_native_binding
    )
    return key


async def test_live_future_queue_and_foreign_ingress_do_not_change_summary_source(owner):
    agent, turn, bridge, witness, source = owner
    key = await queue(agent, turn.session_id)
    assert await capture(owner) == source
    comms = agent._comms
    comms.threads.claim_thread("foreign", tags=frozenset(), worktree=str(comms.root))
    comms.threads.claim_thread("another", tags=frozenset(), worktree=str(comms.root))
    comms.messaging.send("foreign", "another", "unrelated")
    agent.inputs.dispositions.record(
        "acp:foreign",
        seq=None,
        owner="foreign",
        admission=1,
        target="foreign",
        text="foreign input",
    )
    assert await capture(owner) == source
    assert agent.inputs.dispositions.read().rows.get(key).declared_name == "reserved"


@pytest.mark.parametrize(
    "change",
    [
        "steer",
        "clear",
        "promote",
        "shutdown",
        "lost_owner",
        "changed_queue",
        "deleted_queue",
        "bound_queue",
        "old_unknown",
    ],
)
async def test_uncertain_or_changed_input_never_borrows_future_queue_exception(owner, change):
    agent, turn, bridge, witness, source = owner
    current = turn.thread
    key = await queue(agent, turn.session_id)
    if change == "steer":
        await queue(agent, turn.session_id, delivery=SteerPromptRequest)
    elif change == "clear":
        await agent.inputs.clear_queued_inputs(turn.session_id)
    elif change == "promote":
        await agent.prompt(turn.session_id, [], _meta=encode_request(SendNowRequest()))
    elif change == "shutdown":
        await agent.inputs.close()
    elif change == "lost_owner":
        other = CommsAgent(agent._comms, auto_wake=False)
        await other.inputs.close()
        bridge.boundary = replace(bridge.boundary, future_queue=other.inputs)
    elif change in {"changed_queue", "deleted_queue"}:

        def mutate(document):
            rows = dict(document.rows)
            if change == "changed_queue":
                rows[key] = replace(rows[key], source_text="changed")
            else:
                del rows[key]
            return replace(document, rows=rows)

        agent.inputs.dispositions.update(mutate)
    elif change == "bound_queue":
        agent.inputs.dispositions.bind(
            key,
            admission=current.active_turn.admission_generation,
            turn_id=turn.turn_id,
            native_id="a" * 32,
            text="already attempted",
        )
    elif change == "old_unknown":
        agent.inputs.dispositions.record(
            "acp:old",
            seq=None,
            owner=current.name,
            admission=current.active_turn.admission_generation,
            target=current.name,
            text="old",
        )
        assert agent.inputs.dispositions.bind(
            "acp:old",
            admission=current.active_turn.admission_generation,
            turn_id="earlier",
            native_id="b" * 32,
            text="old",
        )
        assert agent.inputs.dispositions.read().rows["acp:old"].declared_name == "bound_unknown"
    with pytest.raises(RelationViolationError):
        await capture(owner)
    assert agent.inputs.dispositions.read().rows[turn.original_keys[0]].declared_name == "reserved"
    assert (
        CompactionJournal(agent._comms.root / "compaction-commits.sqlite3").operations.unresolved(
            str(current.session_file)
        )
        == ()
    )


@pytest.mark.parametrize("change", ["original", "bus", "owner", "turn"])
async def test_relevant_source_and_owner_fences_remain(owner, change):
    agent, turn, bridge, witness, source = owner
    current = turn.thread
    await queue(agent, turn.session_id)
    comms = agent._comms
    if change == "original":
        agent.inputs.dispositions.update(
            lambda document: replace(
                document,
                rows={
                    **document.rows,
                    turn.original_keys[0]: replace(document.rows[turn.original_keys[0]], source_text="corrected"),
                },
            )
        )
        assert await capture(owner) != source
    elif change == "bus":
        comms.threads.claim_thread("peer", tags=frozenset(), worktree=str(comms.root))
        comms.messaging.send("peer", current.name, "correction")
        assert await capture(owner) != source
    elif change == "owner":
        foreign = ParentedProcess.launch((sys.executable, "-c", "import time; time.sleep(30)"))
        try:
            comms.registry.register(
                replace(current, process_identity=foreign.identity, active_turn=None)
            )
            with pytest.raises((RelationViolationError, ValueError)):
                await capture(owner)
        finally:
            foreign.stop_sync()
    else:
        assert comms.agents.finish_turn(turn.turn_lease) is not None
        with pytest.raises((RelationViolationError, ValueError)):
            await capture(owner)


@pytest.mark.parametrize("terminal", [False, True])
async def test_unstarted_input_failure_restores_exact_original_without_replay(native_backend, terminal):
    native = native_backend
    await native.author_history()
    saved = native.session.read_bytes()
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    async with native.open_owner() as (agent, session_id):
        async with native.original_input(agent, session_id, "lost prompt") as turn:
            if terminal:
                # The original native failure owner settles unbound input before
                # reporting its terminal error; error observation alone does not.
                await Coordination.run_worker(turn.settle_unbound)
            event = (events.Done("Pi preflight ended before attestation", False)
                     if terminal else events.Error("Pi preflight ended before attestation"))
            await agent._emit_event(session_id, event, Client())
            failed = tuple(fact for fact in decode_updates(updates[-1].field_meta)
                           if isinstance(fact, InputFailedUpdate))
            assert len(failed) == 1 and failed[0].text == "lost prompt"
            assert failed[0].failure.description == "Pi preflight ended before attestation"
            key = turn.original_keys[0]
            original = agent.inputs.dispositions.read().lookup(key)
            assert original.declared_name == ("not_sent" if terminal else "reserved")
            assert not original.has_native_binding
        # Retiring that acquired turn preserves its input for explicit recovery.
        preserved = agent.inputs.dispositions.read().lookup(key)
        assert preserved.declared_name == "not_sent"
        async with native.original_input(agent, session_id, "distinct explicit input") as next_turn:
            assert next_turn.original_keys != (key,)
            await agent._emit_event(session_id, events.Error("distinct preflight failure"), Client())
            failed = tuple(fact for fact in decode_updates(updates[-1].field_meta)
                           if isinstance(fact, InputFailedUpdate))
            assert len(failed) == 1 and failed[0].text == "distinct explicit input"
            assert agent.inputs.dispositions.read().lookup(key) == preserved
    assert native.session.read_bytes() == saved and native.provider.posts == 0
