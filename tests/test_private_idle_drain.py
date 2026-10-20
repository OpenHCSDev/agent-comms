"""Idle observations avoid repeated work without becoming delivery authority."""

from bus_history import read_history
from agent_comms.native_package import VerifiedNativePackage
from conftest import await_event_or_task, publish_initial
from agent_comms.messages import NoticeRole
import asyncio
import os
import threading
from contextlib import AsyncExitStack
from dataclasses import replace

import pytest

import agent_comms.acp as acp
import agent_comms.cohort_foreground as cohort_foreground
import agent_comms.coordinated_runtime as coordinated_runtime
import agent_comms.native_pi as native_pi
import agent_comms.private_nk_entrypoint as private_nk_entrypoint
import agent_comms.agent_events as events
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.coordinator import Coordination
from agent_comms.coordination_cohort import next_sealed_assignment
from agent_comms.native_source_cursor import NativeSourceCursor
from agent_comms.child_process import ProcessIdentity
from agent_comms.owned_turn import OwnedTurn
from agent_comms.messages import AlertKind, InfoKind, Message
from agent_comms.threads import Thread
from agent_comms.private_path import FileRevision
from agent_comms.store_files import StoreLockContention, _store_lock
from test_acp_private_nk_delivery import _session
from test_coordinated_runtime import _root, tmp_path  # noqa: F401


@pytest.mark.asyncio
async def test_checked_observation_waits_and_joins_its_owned_worker(tmp_path):
    comms, agent, root_id = _session(tmp_path)
    log = comms.bus.log
    # A busy original source is pending, not an unavailable inbox. Cancelling
    # one waiter must leave the holder and the other observations intact.
    with _store_lock(log.path):
        tasks = [asyncio.create_task(agent.inputs._observe_private_revision("beta", root_id))
                 for _ in range(3)]
        await asyncio.sleep(0.08)
        assert not any(task.done() for task in tasks)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert not any(task.done() for task in tasks[1:])
    observed = await asyncio.wait_for(asyncio.gather(*tasks[1:]), 5)
    assert observed[0] == observed[1]

    # Once acquired, cancellation must join the original callback before its
    # descriptor closes. The worker consumes the prefix record off the loop.
    entered, finish = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()

    def consume(source):
        assert threading.get_ident() != loop_thread
        source.require_current()
        loop.call_soon_threadsafe(entered.set)
        assert finish.wait(5)
        return source.committed_sequence()

    task = asyncio.create_task(log.read_checked_async(consume))
    try:
        await await_event_or_task(entered.wait(), task)
        task.cancel()
        await asyncio.sleep(0.04)
        assert not task.done()
        with pytest.raises(BlockingIOError):
            with _store_lock(log.path, blocking=False):
                pass
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    with log.checked_read(blocking=False) as source:
        source.require_current()

    # A completed read must release physical ownership before its result is
    # delivered to the event loop. Otherwise synchronous final publication
    # waits on this same process while preventing the awaited file close.
    entered.clear()
    finish.clear()
    task = asyncio.create_task(log.read_checked_async(consume))
    await await_event_or_task(entered.wait(), task)
    finish.set()
    # This synchronous wait keeps the loop from processing the completed
    # Future; the worker must release the real lock regardless.
    with log.checked_read(contention=StoreLockContention(5.0)) as source:
        source.require_current()
    assert not task.done()
    await task

    # Callback refusal remains the original error and releases its resource.
    failure = ValueError("original consumer refusal")

    def refuse(source):
        source.require_current()
        raise failure

    with pytest.raises(ValueError) as raised:
        await log.read_checked_async(refuse)
    assert raised.value is failure
    with log.checked_read(blocking=False) as source:
        source.require_current()


@pytest.mark.asyncio
async def test_empty_delivery_recovery_does_not_wait_for_wire_writer(tmp_path):
    from agent_comms.queued_input import DeliveredInput

    comms, agent, root_id = _covered_session(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def writer():
        with _store_lock(comms._wire_lock_path):
            entered.set()
            assert release.wait(5)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert entered.wait(2)
    task = asyncio.create_task(DeliveredInput.recover_interrupted(agent.inputs, "beta", root_id))
    try:
        # The real write lock is held; an empty recovery must finish without it.
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done
        assert task.result() == 0
    finally:
        release.set()
        writer_thread.join(5)
        await task


@pytest.mark.asyncio
async def test_turn_publication_joins_cancellation_before_lease_release(tmp_path):
    comms, agent, _root_id = _session(tmp_path)
    origin = Message("user", "#team", "publication ownership", InfoKind)
    execution = OwnedTurn(
        agent.turns, "beta", "beta", "publication ownership", origins=(origin,)
    )
    async with AsyncExitStack() as resources:
        async with AsyncExitStack() as permits:
            assert await execution.acquire(resources, permits)
            progress = execution.progress
            # Only native events are supplied. Registry, original input,
            # wire notices, diagnostic and final checkpoint use real owners.
            await progress.chunk(events.Chunk("committed progress"))
            await progress.committed_progress(events.CommittedProgress("committed progress"))
            assert progress.reply_parts == []
            assert [(row.body, isinstance(row.role, NoticeRole)) for row in read_history(comms)] == [
                ("committed progress", True),
            ]
            progress.result = events.Done("refused", False)
            # A real writer holds the bus, so the failure notice's worker waits
            # on the flock while the turn is cancelled.
            with _store_lock(comms.root / "bus.jsonl"):
                task = asyncio.create_task(progress.publish_result())
                await asyncio.sleep(0.04)
                task.cancel()
                await asyncio.sleep(0.04)
                assert not task.done()
                assert comms.registry.require("beta").turn_lease == execution.turn_lease
            with pytest.raises(asyncio.CancelledError):
                await task
            rows = read_history(comms)
            assert len(rows) == 2 and isinstance(rows[-1].role, NoticeRole)
            assert rows[-1].type is AlertKind
            assert comms.registry.require("beta").turn_lease == execution.turn_lease
    assert not comms.registry.require("beta").executing


def _covered_session(tmp_path):
    comms, agent, root_id = _session(tmp_path)
    other = comms.registry.new_thread('alpha', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(other)
    with Coordination(comms.root / "coordination.sqlite3") as store:
        store.participants.register(
            RecordedThreadIncarnation("fixture", other.created_at).lookup, other.name, other.name, committed=True,
        )
    publish_initial(comms, "sender", other.name, "another recipient's original source")
    return comms, agent, root_id


@pytest.mark.asyncio
async def test_quiescent_private_drain_does_no_package_or_repeated_cursor_work(
    tmp_path, monkeypatch
):
    comms, agent, _root_id = _covered_session(tmp_path)
    # An empty cursor still requires recovery. A genuine absent-audience
    # initial supplies a coverage-only cursor without a native input.
    counts = {"accept": 0, "cursor": 0}
    accept, cursor = acp._accept_visible_deliveries, NativeSourceCursor.advance

    async def accepted(*args, **kwargs):
        counts["accept"] += 1
        return await accept(*args, **kwargs)

    def covered(*args, **kwargs):
        counts["cursor"] += 1
        return cursor(*args, **kwargs)

    def no_package(_path):
        raise AssertionError("idle observation must not hash the native package")

    monkeypatch.setattr(acp, "_accept_visible_deliveries", accepted)
    monkeypatch.setattr(NativeSourceCursor, "advance", covered)
    monkeypatch.setattr(VerifiedNativePackage, "acquire", classmethod(lambda cls, path: no_package(path)))
    monkeypatch.setattr(VerifiedNativePackage, "acquire", classmethod(lambda cls, path: no_package(path)))
    assert await agent.inputs.drain_inbox("beta") == 0
    # Advancing the durable cursor changes the first before/after observation.
    assert await agent.inputs.drain_inbox("beta") == 0
    initial = counts.copy()
    for _ in range(20):
        assert await agent.inputs.drain_inbox("beta") == 0
    assert counts == initial == {"accept": 2, "cursor": 2}
    # The shared files also carry other owners. Their declarations and SQL
    # registrations cannot invalidate beta's quiescent work observation.
    other = comms.registry.require("sender")
    comms.registry.register(replace(other, task="other owner changed"))
    with Coordination(comms.root / "coordination.sqlite3") as store:
        store.participants.register("unrelated", "unrelated", "unrelated", committed=True)
    assert await agent.inputs.drain_inbox("beta") == 0
    assert counts == initial
    # A real owner revision is still observed; it grants no native execution.
    owner = comms.registry.require("beta")
    comms.registry.register(replace(owner, task="changed task"))
    assert await agent.inputs.drain_inbox("beta") == 0
    assert counts == {"accept": 3, "cursor": 3}


@pytest.mark.asyncio
async def test_settled_empty_cursor_is_not_rescanned_while_nothing_changes(
    tmp_path, monkeypatch
):
    comms, agent, _root_id = _session(tmp_path)
    # No source proves a cursor for this claim_scope, so every advance settles
    # as an empty observation. Rescanning it cannot change that answer until
    # the owner's observation revision changes.
    calls = []
    advance = NativeSourceCursor.advance

    def counted(*args, **kwargs):
        calls.append(args)
        return advance(*args, **kwargs)

    monkeypatch.setattr(NativeSourceCursor, "advance", counted)
    assert await agent.inputs.drain_inbox("beta") == 0
    settled = len(calls)
    assert settled >= 1
    for _ in range(20):
        assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == settled, "idle observation re-advanced a settled empty cursor"
    # A real revision of this owner advances once more.
    owner = comms.registry.require("beta")
    comms.registry.register(replace(owner, task="changed task"))
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == settled + 1


@pytest.mark.asyncio
async def test_runtime_configuration_invalidates_idle_observation(tmp_path, monkeypatch):
    _comms, agent, _root_id = _session(tmp_path)
    calls = []

    async def observed(*args):
        calls.append(args)
        return 0

    monkeypatch.setattr(agent, "_drain_private_nk", observed)
    assert await agent.inputs.drain_inbox("beta") == 0
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 1
    agent.inputs.auto_wake = False
    assert await agent.inputs.drain_inbox("beta") == 0
    agent.inputs.auto_wake = True
    assert await agent.inputs.drain_inbox("beta") == 0
    agent.sessions.runtime_enabled = False
    assert await agent.inputs.drain_inbox("beta") == 0
    agent.sessions.runtime_enabled = True
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 5


def test_coordination_read_preserves_revision_and_repairs_exposed_mode(tmp_path):
    path = tmp_path / "coordination.sqlite3"
    with Coordination(path):
        pass
    revision = FileRevision.capture(path)
    with Coordination(path):
        pass
    assert FileRevision.capture(path) == revision
    path.chmod(0o644)
    with Coordination(path):
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_new_inputs_and_recovery_revision_invalidate_idle_observation(tmp_path, monkeypatch):
    comms, agent, _root_id = _covered_session(tmp_path)
    calls = []
    original = agent._drain_private_nk

    async def observed(*args):
        calls.append(args)
        return await original(*args)

    monkeypatch.setattr(agent, "_drain_private_nk", observed)
    assert await agent.inputs.drain_inbox("beta") == 0
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 2
    with Coordination(comms.root / "coordination.sqlite3") as store:
        # Another participant's SQL work is unrelated to this recipient.
        store.participants.register("new", "new", "new", committed=True)
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 2
    with Coordination(comms.root / "coordination.sqlite3") as store:
        # Actual recovery/reassignment of beta must still invalidate the
        # observation without a wire append or a provider request.
        beta = comms.registry.require("beta")
        person = store.participants.get(RecordedThreadIncarnation("fixture", beta.created_at).lookup)
        store.participants.advance_generation(
            person.lookup, beta.name, expected_generation=person.participant_generation,
        )
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 3
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 4
    # A new original message cut must invalidate even when beta has no work.
    first = publish_initial(comms, "sender", "alpha", "new absent-audience source")
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 5
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 6
    before = agent.inputs.state("beta").idle_revision
    second = publish_initial(comms, "sender", "beta", "new pending input")
    assert first.seq < second.seq
    with Coordination(comms.root / "coordination.sqlite3") as store:
        await cohort_foreground._accept_visible_deliveries(
            comms.bus, _root_id, store.session.path, RecordedThreadIncarnation("fixture", beta.created_at).lookup, 0,
            owner_name=beta.name,
        )
        pending = next_sealed_assignment(
            store, RecordedThreadIncarnation("fixture", beta.created_at).lookup, beta.name,
        )
        assert pending.wire_seq == second.seq
    assert await agent.inputs._observe_private_revision("beta", _root_id) != before
    # This control observes actual delivery/recovery facts. It neither mocks
    # native execution nor grants a provider call to the pending input.


@pytest.mark.asyncio
async def test_during_scan_change_is_not_absorbed_as_idle(tmp_path, monkeypatch):
    comms, agent, _root_id = _session(tmp_path)
    entered, resume = asyncio.Event(), asyncio.Event()
    calls = []

    async def paused(*args):
        calls.append(args)
        entered.set()
        await resume.wait()
        return 0

    monkeypatch.setattr(agent, "_drain_private_nk", paused)
    task = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    await entered.wait()
    publish_initial(comms, "sender", "beta", "arrived during observation")
    resume.set()
    assert await task == 0
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 2
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_sealed_cohorts_are_not_reaccepted_or_rewritten(tmp_path, monkeypatch):
    root, root_id, comms, original, people = _root(tmp_path, direct=True)
    lookup = RecordedThreadIncarnation("fixture", people[2].created_at).lookup
    accepted = []
    accept = cohort_foreground.accept_delivery_cohort

    def observed(bus, root_id, sequence, store):
        accepted.append(sequence)
        return accept(bus, root_id, sequence, store)

    monkeypatch.setattr(cohort_foreground, "accept_delivery_cohort", observed)
    statements = []
    original_init = Coordination.__init__

    def traced_init(resource, *args, **kwargs):
        original_init(resource, *args, **kwargs)
        resource.session._connection.set_trace_callback(statements.append)

    # Trace the worker's actual connection as well as the live caller's.
    monkeypatch.setattr(Coordination, "__init__", traced_init)
    with Coordination(root / "coordination.sqlite3") as store:
        assert (
            await cohort_foreground._accept_visible_deliveries(
                comms.bus, root_id, store.session.path, lookup, 0, owner_name="beta"
            )
            == original.message.seq
        )
        assert not accepted
        assert not any("BEGIN IMMEDIATE" in sql for sql in statements)
        message = publish_initial(comms, "sender", "beta", "unaccepted source")
        await cohort_foreground._accept_visible_deliveries(
            comms.bus, root_id, store.session.path, lookup, 0, owner_name="beta"
        )
        assert accepted == [message.seq]
        statements.clear()
        await cohort_foreground._accept_visible_deliveries(
            comms.bus, root_id, store.session.path, lookup, 0, owner_name="beta"
        )
        assert accepted == [message.seq]
        assert not any("BEGIN IMMEDIATE" in sql for sql in statements)


