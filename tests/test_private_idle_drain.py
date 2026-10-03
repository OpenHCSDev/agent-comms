"""Idle observations avoid repeated work without becoming delivery authority."""

import asyncio
import os
from dataclasses import replace

import pytest

from agent_comms import acp, cohort_foreground, coordinated_runtime
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.coordinator import Coordination
from agent_comms.coordination_cohort import next_sealed_assignment
from agent_comms.child_process import ProcessIdentity
from agent_comms.threads import Thread
from agent_comms.store_files import file_revision
from test_acp_private_nk_delivery import _session
from test_coordinated_runtime import _root, tmp_path  # noqa: F401


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
    accept, cursor = acp._accept_visible_deliveries, acp.NativeSourceCursor.advance

    async def accepted(*args, **kwargs):
        counts["accept"] += 1
        return await accept(*args, **kwargs)

    def covered(*args, **kwargs):
        counts["cursor"] += 1
        return cursor(*args, **kwargs)

    def no_package(_path):
        raise AssertionError("idle observation must not hash the native package")

    monkeypatch.setattr(acp, "_accept_visible_deliveries", accepted)
    monkeypatch.setattr(acp.NativeSourceCursor, "advance", covered)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", no_package)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", no_package)
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
            comms.bus, _root_id, store, stable_thread_lookup(beta.created_at), 0,
            owner_name=beta.name,
        )
        pending = next_sealed_assignment(
            store, stable_thread_lookup(beta.created_at), beta.name,
        )
        assert pending.wire_seq == second.seq
        assert agent.inputs._private_observation_revision("beta", _root_id, store) != before
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
                comms.bus, root_id, store, lookup, 0, owner_name="beta"
            )
            == original.message.seq
        )
        assert not accepted
        assert not any("BEGIN IMMEDIATE" in sql for sql in statements)
        message = comms.messaging.send_initial_cohort("sender", "beta", "unaccepted source")
        await cohort_foreground._accept_visible_deliveries(
            comms.bus, root_id, store, lookup, 0, owner_name="beta"
        )
        assert accepted == [message.seq]
        statements.clear()
        await cohort_foreground._accept_visible_deliveries(
            comms.bus, root_id, store, lookup, 0, owner_name="beta"
        )
        assert accepted == [message.seq]
        assert not any("BEGIN IMMEDIATE" in sql for sql in statements)
