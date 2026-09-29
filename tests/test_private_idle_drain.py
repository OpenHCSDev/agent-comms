"""Idle observations avoid repeated work without becoming delivery authority."""

import asyncio
from dataclasses import replace

import pytest

from agent_comms import acp, cohort_foreground, coordinated_runtime
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.coordination_store import MutationStore
from agent_comms.store_files import file_revision
from agent_comms.tracked_turn import TrackedTurnSession
from test_acp_private_nk_delivery import _session
from test_coordinated_runtime import _fake_model, _root, tmp_path  # noqa: F401


@pytest.mark.asyncio
async def test_quiescent_private_drain_does_no_package_or_repeated_cursor_work(
    tmp_path, monkeypatch
):
    comms, agent, _root_id = _session(tmp_path)
    counts = {"accept": 0, "cursor": 0}
    accept, cursor = acp._accept_visible_initials, acp.advance_current_native_cursor

    def accepted(*args, **kwargs):
        counts["accept"] += 1
        return accept(*args, **kwargs)

    def covered(*args, **kwargs):
        counts["cursor"] += 1
        return cursor(*args, **kwargs)

    def no_package(_path):
        raise AssertionError("idle observation must not hash the native package")

    monkeypatch.setattr(acp, "_accept_visible_initials", accepted)
    monkeypatch.setattr(acp, "advance_current_native_cursor", covered)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", no_package)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", no_package)
    assert await agent.inputs.drain_inbox("beta") == 0
    initial = counts.copy()
    for _ in range(20):
        assert await agent.inputs.drain_inbox("beta") == 0
    assert counts == initial == {"accept": 1, "cursor": 1}
    # A real owner revision is still observed; it grants no native execution.
    owner = comms.registry.require("beta")
    comms.registry.register(replace(owner, task="changed task"))
    assert await agent.inputs.drain_inbox("beta") == 0
    assert counts == {"accept": 2, "cursor": 2}


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
    with MutationStore(path):
        pass
    revision = file_revision(path)
    with MutationStore(path):
        pass
    assert file_revision(path) == revision
    path.chmod(0o644)
    with MutationStore(path):
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_new_inputs_and_recovery_revision_invalidate_idle_observation(tmp_path, monkeypatch):
    comms, agent, _root_id = _session(tmp_path)
    calls = []
    original = agent._drain_private_nk

    async def observed(*args):
        calls.append(args)
        return await original(*args)

    monkeypatch.setattr(agent, "_drain_private_nk", observed)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, native_calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    assert await agent.inputs.drain_inbox("beta") == 0
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 1
    with MutationStore(comms.root / "coordination.sqlite3") as store:
        # A coordinator-only change (e.g. recovery) has no watched file event.
        # The unchanged periodic fallback must nevertheless notice its revision.
        store.register_participant("new", "new", "new", committed=True)
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 2
    first = comms.messaging.send_initial_cohort("sender", "beta", "first new input")
    second = comms.messaging.send_initial_cohort("sender", "beta", "second new input")
    assert first.seq < second.seq
    assert await agent.inputs.drain_inbox("beta") == 1
    assert await agent.inputs.drain_inbox("beta") == 1
    assert len(native_calls) == 2
    assert native_calls[0][0] != native_calls[1][0]


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


def test_sealed_cohorts_are_not_reaccepted_or_rewritten(tmp_path, monkeypatch):
    root, root_id, comms, original, people = _root(tmp_path, direct=True)
    lookup = stable_thread_lookup(people[2].created_at)
    accepted = []
    accept = cohort_foreground.accept_initial_cohort

    def observed(bus, root_id, sequence, store):
        accepted.append(sequence)
        return accept(bus, root_id, sequence, store)

    monkeypatch.setattr(cohort_foreground, "accept_initial_cohort", observed)
    with MutationStore(root / "coordination.sqlite3") as store:
        statements = []
        store._connection.set_trace_callback(statements.append)
        assert (
            cohort_foreground._accept_visible_initials(
                comms.bus, root_id, store, lookup, 0, owner_name="beta"
            )
            == original.message.seq
        )
        assert not accepted
        assert not any("BEGIN IMMEDIATE" in sql for sql in statements)
        message = comms.messaging.send_initial_cohort("sender", "beta", "unaccepted source")
        cohort_foreground._accept_visible_initials(
            comms.bus, root_id, store, lookup, 0, owner_name="beta"
        )
        assert accepted == [message.seq]
        statements.clear()
        cohort_foreground._accept_visible_initials(
            comms.bus, root_id, store, lookup, 0, owner_name="beta"
        )
        assert accepted == [message.seq]
        assert not any("BEGIN IMMEDIATE" in sql for sql in statements)
