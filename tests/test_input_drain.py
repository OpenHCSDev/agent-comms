"""Owned queue receipts and the real compaction source boundary, without providers."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.declarations import RelationViolationError, Thread, _store_lock
from agent_comms.input_disposition import InputDispositions
from agent_comms.owner_compaction_commit import OwnerCompactionCommit


@pytest.fixture
async def owner(tmp_path, monkeypatch):
    comms = Comms(tmp_path)
    comms.threads.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    session = tmp_path / "saved.jsonl"
    session.write_text("saved history\n")
    comms.registry.register(replace(comms.registry.require("owner"), session_file=str(session)))
    current, epoch = comms.registry.live_owner_with_generation("owner")
    current, epoch = comms.registry.claim_live_turn_with_generation(
        current, "turn", expected_owner_generation=epoch
    )
    agent = CommsAgent(comms, auto_wake=False)
    agent.sessions.bindings["owner"] = "owner"
    agent.inputs.backend_inboxes["owner"] = asyncio.Queue()
    agent.turns.active_turns["owner"] = "turn"
    agent.inputs.turn_original_input_keys["owner"] = ("acp:original",)
    agent.inputs.dispositions.record(
        "acp:original",
        seq=None,
        owner="owner",
        admission=current.active_turn.admission_generation,
        target="owner",
        text="original",
    )
    # Only native package verification is outside this source-boundary fixture.
    # The actual registry, wire, disposition, session locks and source CAS run.
    monkeypatch.setattr(OwnerCompactionCommit, "_verify_native", lambda self: None)
    bridge = OwnerCompactionCommit(comms.registry.store.path, tmp_path, future_queue=agent.inputs)
    witness = dict(
        sessionId="saved",
        sessionFile=str(session),
        leafId="leaf",
        firstKeptEntryId="leaf",
        revision="native-revision",
    )
    source = bridge.capture_source(current, epoch, witness, pending_input_key="acp:original")
    yield agent, current, epoch, bridge, witness, source
    await agent.shutdown()


async def queue(agent, text="future", delivery="queue"):
    response = await agent.prompt(
        "owner",
        [{"type": "text", "text": text}],
        agentComms={"delivery": delivery, "deferDisplay": True},
    )
    public = response.field_meta["agentComms"]["inputDisposition"]
    assert public["status"] == "accepted_not_started"
    key = "acp:" + public["inputId"]
    assert InputDispositions(agent._comms.root).get(key)["native_id"] is None
    return key


def capture(fixture):
    agent, current, epoch, bridge, witness, source = fixture
    return bridge.capture_source(current, epoch, witness, pending_input_key="acp:original")


async def test_live_future_queue_and_foreign_ingress_do_not_change_summary_source(owner):
    agent, current, epoch, bridge, witness, source = owner
    key = await queue(agent)
    assert capture(owner) == source
    comms = agent._comms
    comms.threads.register(Thread("foreign", frozenset(), str(comms.root)))
    comms.threads.register(Thread("another", frozenset(), str(comms.root)))
    comms.messaging.send("foreign", "another", "unrelated")
    agent.inputs.dispositions.record(
        "acp:foreign",
        seq=None,
        owner="foreign",
        admission=1,
        target="foreign",
        text="foreign input",
    )
    assert capture(owner) == source
    assert agent.inputs.dispositions.get(key)["status"] == "unknown"
    assert not any(
        name in vars(agent) for name in ("_queued_inputs", "_dispositions", "_drain_tasks")
    )
    assert not hasattr(agent, "_queued_inputs")


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
    agent, current, epoch, bridge, witness, source = owner
    key = await queue(agent)
    if change == "steer":
        await queue(agent, delivery="steer")
    elif change == "clear":
        await agent.inputs.clear_queued_inputs("owner")
    elif change == "promote":
        await agent.prompt("owner", [], agentComms={"sendNow": True})
    elif change == "shutdown":
        await agent.inputs.stop_wakes()
    elif change == "lost_owner":
        bridge.future_queue = CommsAgent(agent._comms).inputs
    elif change in {"changed_queue", "deleted_queue"}:
        with _store_lock(agent.inputs.dispositions.path):
            rows = agent.inputs.dispositions._read()
            if change == "changed_queue":
                rows[key]["source_text"] = "changed"
            else:
                del rows[key]
            agent.inputs.dispositions._write(rows)
    elif change == "bound_queue":
        agent.inputs.dispositions.bind(
            key,
            admission=current.active_turn.admission_generation,
            turn_id="turn",
            native_id="a" * 32,
            text="already attempted",
        )
    elif change == "old_unknown":
        agent.inputs.dispositions.record(
            "acp:old",
            seq=None,
            owner="owner",
            admission=current.active_turn.admission_generation,
            target="owner",
            text="old",
        )
    with pytest.raises(RelationViolationError):
        capture(owner)
    assert agent.inputs.dispositions.status("acp:original") == "unknown"
    assert (
        CompactionJournal(agent._comms.root / "compaction-commits.sqlite3").unresolved(
            str(current.session_file)
        )
        == ()
    )


@pytest.mark.parametrize("change", ["original", "bus", "owner", "turn"])
async def test_relevant_source_and_owner_fences_remain(owner, change):
    agent, current, epoch, bridge, witness, source = owner
    await queue(agent)
    comms = agent._comms
    if change == "original":
        with _store_lock(agent.inputs.dispositions.path):
            rows = agent.inputs.dispositions._read()
            rows["acp:original"]["source_text"] = "corrected"
            agent.inputs.dispositions._write(rows)
        assert capture(owner) != source
    elif change == "bus":
        comms.threads.register(Thread("peer", frozenset(), str(comms.root)))
        comms.messaging.send("peer", "owner", "correction")
        assert capture(owner) != source
    else:
        comms.registry.register(
            replace(current, pid=12345, active_turn=None)
            if change == "owner"
            else replace(current, active_turn=None)
        )
        with pytest.raises((RelationViolationError, ValueError)):
            capture(owner)
