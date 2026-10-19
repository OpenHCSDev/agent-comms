"""Idle observations avoid repeated work without becoming delivery authority."""

from agent_comms.native_package import VerifiedNativePackage
from conftest import publish_initial
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
from agent_comms.messages import InfoKind, Message
from agent_comms.threads import Thread
from agent_comms.private_path import FileRevision
from agent_comms.store_files import _store_lock
from test_acp_private_nk_delivery import _session
from test_coordinated_runtime import _root, tmp_path  # noqa: F401
from agent_comms.channels import ANY_CHANNEL


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

    # A completed read must release physical ownership before its result is
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
async def test_turn_publication_joins_cancellation_before_lease_release(tmp_path, monkeypatch):
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
            assert [(row.body, isinstance(row.role, NoticeRole)) for row in list(comms.views.history_page(ANY_CHANNEL).messages)] == [
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
            rows = list(comms.views.history_page(ANY_CHANNEL).messages)
            assert len(rows) == 2 and isinstance(rows[-1].role, NoticeRole)
            assert "[Open diagnostic]" in rows[-1].body
            assert comms.registry.require("beta").turn_lease == execution.turn_lease
    assert not comms.registry.require("beta").active_turn.executing


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
async def test_unchanged_observation_tick_skips_all_store_reads(tmp_path, monkeypatch):
    import agent_comms.input_drain as input_drain

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
        activity_of = comms.agents.activity_of

        def no_idle_activity(thread, *, snapshot):
            raise AssertionError("an unchanged tick reread activity and the registry")

        monkeypatch.setattr(comms.agents, "activity_of", no_idle_activity)
        for _ in range(20):
            await tick()
        monkeypatch.setattr(comms.agents, "activity_of", activity_of)
        assert len(drains) == settled, "an unchanged tick re-read the stores"
        assert len(syncs) == settled
        # Another thread's declaration, change, stop, coordination row and removal
        # rewrite the shared stores but not beta's part: the tick decodes only
        # beta's registry entry and runs nothing else.
        from agent_comms.registry_document import RegistryDocument

        decoded = []
        decode = RegistryDocument.from_wire.__func__

        def counted_decode(cls, raw):
            decoded.append(len(raw["threads"]))
            return decode(cls, raw)

        monkeypatch.setattr(RegistryDocument, "from_wire", classmethod(counted_decode))

        async def changed_elsewhere(change):
            change()
            # Another process wrote it: this process holds no decoded revision.
            comms.registry.store.cache.entry = None
            del decoded[:]
            await tick()
            assert set(decoded) <= {1}, "the tick decoded other threads' entries"

        await changed_elsewhere(
            lambda: comms.registry.declare(comms.registry.new_thread('unrelated', frozenset(), str(tmp_path))))
        other = comms.registry.require("sender")
        await changed_elsewhere(
            lambda: comms.registry.register(replace(other, task="other owner changed")))
        await changed_elsewhere(lambda: comms.owners.stop("unrelated"))

        def registered_elsewhere():
            with Coordination(comms.root / "coordination.sqlite3") as store:
                store.participants.register("unrelated", "unrelated", "unrelated", committed=True)

        # Only the coordination store changed: beta rereads its own rows there,
        # not the unchanged bus log or registry.
        certified = []
        read_certified = comms.bus.log.read_certified_async

        async def counted_certified(*args, **kwargs):
            certified.append(args)
            return await read_certified(*args, **kwargs)

        monkeypatch.setattr(comms.bus.log, "read_certified_async", counted_certified)
        await changed_elsewhere(registered_elsewhere)
        assert certified == [] and decoded == [], "an unchanged bus or registry was reread"
        await changed_elsewhere(lambda: comms.threads.delete("unrelated"))
        assert len(drains) == settled, "another thread's registry change ran the tick"
        # A wake can stat the registry while another owner's write is still in
        # flight. The entry read waits for that write, so the tick settles at
        # the revision it read; the write's later wakes reread nothing.
        in_flight = (None, None)
        tick_revision = agent.inputs._tick_revision
        monkeypatch.setattr(agent.inputs, "_tick_revision",
                            lambda sid: replace(tick_revision(sid), registry=in_flight))
        comms.registry.register(replace(comms.registry.require("sender"), task="written"))
        comms.registry.store.cache.entry = None
        del decoded[:]
        await tick()
        monkeypatch.setattr(agent.inputs, "_tick_revision", tick_revision)
        await tick()
        assert decoded == [1], "a later wake of one write reread the entry"
        assert len(drains) == settled
        # Any change of this owner's own entry runs the full observation again.
        owner = comms.registry.require("beta")
        comms.registry.register(replace(owner, task="changed task"))
        await tick()
        assert len(drains) == settled + 1
        # No activity append changes a settled tick: only this owner's tick
        # writes its drain diagnostic and every other write carries it forward.
        from agent_comms.activity import ActivityState

        comms.agents.set_activity("sender", ActivityState.WORKING, "other owner's turn")
        await tick()
        comms.agents.set_activity("sender", ActivityState.IDLE, "")
        await tick()
        comms.agents.set_activity("beta", ActivityState.WORKING, "written elsewhere")
        await tick()
        assert len(drains) == settled + 1, "an activity append ran the tick"
        # A standby's wait graph reads other threads' entries: any registry
        # change runs its tick, including closed-group recovery. Nothing on a
        # clock does.
        from agent_comms.goal_presentation import (
            GoalExecution, GoalExecutionState, GoalSnapshot, GoalWaitTarget,
        )

        recovered = []
        monkeypatch.setattr(comms.goals, "recover_closed_goal_wait", recovered.append)
        sender = comms.registry.require("sender")
        agent.inputs.effects.turns.goals.goal_snapshots["beta"] = GoalSnapshot(None, GoalExecution(
            GoalExecutionState.STANDBY, "goal", (GoalWaitTarget(sender.incarnation.require_recorded()),),
        ))
        comms.registry.declare(comms.registry.new_thread('unrelated', frozenset(), str(tmp_path)))
        await tick()
        assert len(drains) == settled + 2 and recovered == ["beta"]
        for _ in range(3):
            await tick()
        assert len(drains) == settled + 2 and recovered == ["beta"]
        agent.inputs.effects.turns.goals.goal_snapshots.pop("beta")
        # Process-local work waiting on the next tick is never skipped.
        for _ in range(2):
            await tick()
        from agent_comms.routing import ScheduledTurn
        from agent_comms.schedule_rules import WakeScheduleCheck

        # The observer owns this check; a real queued turn is dispatched by
        # another owner, whose separate tests exercise the provider path.
        monkeypatch.setattr(WakeScheduleCheck, "schedule", lambda _check: None)
        before = len(drains)
        agent.inputs.state("beta").pending_turns = [ScheduledTurn("queued locally")]
        await tick()
        assert len(drains) == before + 1
    finally:
        agent.inputs.state("beta").pending_turns = []
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


async def test_bus_appends_for_other_agents_do_not_wake_an_idle_owner(
    tmp_path, monkeypatch
) -> None:
    """A foreign append consults the address owner without decoding bus rows."""
    import agent_comms.input_drain as input_drain
    from agent_comms.private_bus_checkpoint import Addressed

    comms, agent, _root_id = _session(tmp_path)
    comms.registry.declare(
        comms.registry.new_thread('gamma', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )

    ticks, done = asyncio.Queue(), asyncio.Queue()

    async def observations(_root, *_args, **_kwargs):
        while True:
            await ticks.get()
            yield None
            done.put_nowait(None)

    async def tick():
        ticks.put_nowait(None)
        await asyncio.wait_for(done.get(), timeout=10)

    drains = []
    drain = agent.inputs.drain_inbox

    async def counted(session_id):
        drains.append(session_id)
        return await drain(session_id)

    async def no_catalog(session_id):
        pass

    address_reads = []
    observed_after = Addressed.observed_after.__func__

    def measured_address(cls, bus, revision, source, marker_revision, lookup, after_seq):
        address_reads.append((lookup, after_seq, revision))
        return observed_after(cls, bus, revision, source, marker_revision, lookup, after_seq)

    monkeypatch.setattr(input_drain.WireWatch, "observations", staticmethod(observations))
    monkeypatch.setattr(Addressed, "observed_after", classmethod(measured_address))
    monkeypatch.setattr(agent.inputs, "drain_inbox", counted)
    monkeypatch.setattr(agent.sessions.config, "sync_thread", no_catalog)
    observer = asyncio.create_task(agent.inputs.observe("beta"))
    try:
        # Tick until the full tick has settled (its scheduled wake completes);
        # a settled tick answers bus-only changes from the address owner.
        for _ in range(10):
            before = len(drains)
            await tick()
            if len(drains) == before:
                break
        assert drains, "the first tick never drained"
        settled = len(drains)
        comms.messaging.send("sender", "gamma", "for gamma").message
        await tick()  # only shared stores grew, and none of it addresses beta
        assert len(drains) == settled, "another agent's bus append woke an idle owner"
        comms.messaging.send("sender", "gamma", "another for gamma").message
        await tick()
        assert len(drains) == settled
        assert address_reads
        comms.messaging.send("sender", "beta", "for beta").message
        await tick()  # the address record has an unauthorized row for beta: full tick
        assert len(drains) == settled + 1, "a delivered row for the owner did not wake it"
        comms.messaging.send("sender", "gamma", "more for gamma").message
        (comms.root / "private_bus_checkpoint.sqlite3").unlink()
        await tick()  # the address record is unavailable: never invent 'not mine'
        assert len(drains) == settled + 2, "an unavailable address record answered instead of the full tick"
    finally:
        observer.cancel()
