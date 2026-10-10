"""Idle observations avoid repeated work without becoming delivery authority."""

import asyncio
import os
import threading
from contextlib import AsyncExitStack
from dataclasses import replace

import pytest

from agent_comms import acp, cohort_foreground, coordinated_runtime, native_pi
from agent_comms import agent_events as events
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.coordinator import Coordination
from agent_comms.coordination_cohort import next_sealed_assignment
from agent_comms.native_source_cursor import NativeSourceCursor
from agent_comms.child_process import ProcessIdentity
from agent_comms.owned_turn import OwnedTurn
from agent_comms.threads import Thread
from agent_comms.store_files import file_revision
from agent_comms.store_files import _store_lock
from test_acp_private_nk_delivery import _session
from test_coordinated_runtime import _root, tmp_path  # noqa: F401


@pytest.mark.asyncio
async def test_certified_observation_waits_and_joins_its_owned_worker(tmp_path, monkeypatch):
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
    # descriptor closes. The worker consumes the certificate off the loop.
    entered, finish = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def consume(source):
        assert threading.get_ident() != loop_thread
        source.require_current()
        entered.set()
        assert finish.wait(5)
        return source.committed_sequence()

    task = asyncio.create_task(log.read_certified_async(consume))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.01)
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
    with log.certified_read(blocking=False) as source:
        source.require_current()

    # A completed read must release physical custody before its result is
    # delivered to the event loop. Otherwise synchronous final publication
    # waits on this same process while preventing the awaited file close.
    entered.clear()
    finish.clear()
    returned = threading.Event()
    run_worker = Coordination.run_worker

    async def observe_worker(operation):
        def complete():
            try:
                return operation()
            finally:
                returned.set()
        return await run_worker(complete)

    with monkeypatch.context() as observation:
        observation.setattr(Coordination, "run_worker", observe_worker)
        task = asyncio.create_task(log.read_certified_async(consume))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        finish.set()
        # Deliberately keep this loop from processing the completed Future.
        # The real worker/certificate/lock must nevertheless be closed.
        assert returned.wait(5)
        assert not task.done()
        with log.certified_read(blocking=False) as source:
            source.require_current()
        await task

    # Callback refusal remains the original error and releases its resource.
    failure = ValueError("original consumer refusal")

    def refuse(source):
        source.require_current()
        raise failure

    with pytest.raises(ValueError) as raised:
        await log.read_certified_async(refuse)
    assert raised.value is failure
    with log.certified_read(blocking=False) as source:
        source.require_current()


@pytest.mark.asyncio
async def test_turn_publication_joins_cancellation_before_lease_release(tmp_path, monkeypatch):
    comms, agent, _root_id = _session(tmp_path)
    execution = OwnedTurn(agent.turns, "beta", "beta", "publication custody", reply_targets=("#team",))
    async with AsyncExitStack() as resources:
        async with AsyncExitStack() as permits:
            assert await execution.acquire(resources, permits)
            progress = execution.progress
            # Only native events are supplied. Registry, original input,
            # wire notices, diagnostic and final checkpoint use real owners.
            await progress.chunk(events.Chunk("committed progress"))
            await progress.committed_progress(events.CommittedProgress("committed progress"))
            assert progress.reply_parts == []
            assert [(row.body, row.notice) for row in comms.bus.log.full_history()] == [
                ("committed progress", True),
            ]
            progress.result = events.Done("refused", False)
            entered, finish = threading.Event(), threading.Event()
            publish = progress.publish_checkpoint
            loop_thread = threading.get_ident()

            def held_publication():
                assert threading.get_ident() != loop_thread
                entered.set()
                assert finish.wait(5)
                return publish()

            monkeypatch.setattr(progress, "publish_checkpoint", held_publication)
            task = asyncio.create_task(progress.publish_result())
            try:
                while not entered.is_set():
                    await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.sleep(0.04)
                assert not task.done()
                assert comms.registry.require("beta").turn_lease == execution.turn_lease
            finally:
                finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            rows = comms.bus.log.full_history()
            assert len(rows) == 2 and rows[-1].notice
            assert "[Open diagnostic]" in rows[-1].body
            assert comms.registry.require("beta").turn_lease == execution.turn_lease
    assert comms.registry.require("beta").active_turn is None


def _covered_session(tmp_path):
    comms, agent, root_id = _session(tmp_path)
    other = Thread("alpha", frozenset(), str(tmp_path),
                   process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(other)
    with Coordination(comms.root / "coordination.sqlite3") as store:
        store.participants.register(
            stable_thread_lookup(other.created_at), other.name, other.name, committed=True,
        )
    comms.messaging.send_initial_cohort("sender", other.name, "another recipient's original source")
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
    monkeypatch.setattr(cohort_foreground, "_trusted_package", no_package)
    monkeypatch.setattr(native_pi, "_trusted_package", no_package)
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
    # No source proves a cursor for this admission, so every advance settles
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
async def test_unchanged_observation_tick_skips_all_store_reads(tmp_path, monkeypatch):
    from agent_comms import input_drain

    comms, agent, _root_id = _session(tmp_path)
    ticks, done = asyncio.Queue(), asyncio.Queue()

    async def observations(_root, *_args, **_kwargs):
        while True:
            await ticks.get()
            yield None
            done.put_nowait(None)

    async def tick():
        ticks.put_nowait(None)
        await asyncio.wait_for(done.get(), timeout=10)

    drains, syncs = [], []
    drain = agent.inputs.drain_inbox

    async def counted(session_id):
        drains.append(session_id)
        return await drain(session_id)

    async def no_catalog(session_id):
        syncs.append(session_id)

    monkeypatch.setattr(input_drain.WireWatch, "observations", staticmethod(observations))
    monkeypatch.setattr(agent.inputs, "drain_inbox", counted)
    monkeypatch.setattr(agent.sessions.config, "sync_thread", no_catalog)
    observer = asyncio.create_task(agent.inputs.observe("beta"))
    try:
        for _ in range(3):
            await tick()
        settled = len(drains)
        assert settled <= 3
        for _ in range(20):
            await tick()
        assert len(drains) == settled, "an unchanged tick re-read the stores"
        assert len(syncs) == settled
        # Any store change runs the full observation again.
        owner = comms.registry.require("beta")
        comms.registry.register(replace(owner, task="changed task"))
        await tick()
        assert len(drains) == settled + 1
        # Process-local work waiting on the next tick is never skipped.
        for _ in range(2):
            await tick()
        before = len(drains)
        agent.inputs.pending_turns["beta"] = [object()]
        await tick()
        assert len(drains) == before + 1
    finally:
        agent.inputs.pending_turns.pop("beta", None)
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)


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
    agent._private_nk_native_package = tmp_path / "replacement-package"
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 6


def test_coordination_read_preserves_revision_and_repairs_exposed_mode(tmp_path):
    path = tmp_path / "coordination.sqlite3"
    with Coordination(path):
        pass
    revision = file_revision(path)
    with Coordination(path):
        pass
    assert file_revision(path) == revision
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
        person = store.participants.get(stable_thread_lookup(beta.created_at))
        store.participants.advance_generation(
            person.lookup, beta.name, expected_generation=person.participant_generation,
        )
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 3
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 4
    # A new original message cut must invalidate even when beta has no work.
    first = comms.messaging.send_initial_cohort("sender", "alpha", "new absent-audience source")
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 5
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 6
    before = agent.inputs._idle_private_revisions["beta"]
    second = comms.messaging.send_initial_cohort("sender", "beta", "new pending input")
    assert first.seq < second.seq
    with Coordination(comms.root / "coordination.sqlite3") as store:
        await cohort_foreground._accept_visible_deliveries(
            comms.bus, _root_id, store.session.path, stable_thread_lookup(beta.created_at), 0,
            owner_name=beta.name,
        )
        pending = next_sealed_assignment(
            store, stable_thread_lookup(beta.created_at), beta.name,
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
    comms.messaging.send_initial_cohort("sender", "beta", "arrived during observation")
    resume.set()
    assert await task == 0
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 2
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_sealed_cohorts_are_not_reaccepted_or_rewritten(tmp_path, monkeypatch):
    root, root_id, comms, original, people = _root(tmp_path, direct=True)
    lookup = stable_thread_lookup(people[2].created_at)
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
        message = comms.messaging.send_initial_cohort("sender", "beta", "unaccepted source")
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
