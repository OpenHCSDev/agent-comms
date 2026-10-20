"""The actual background drain exposes broken SQLite state and observes recovery."""

import asyncio
import json
import os
import shutil
import sqlite3
import time
from dataclasses import replace

import pytest

from agent_comms.activity import (
    NativeInputDrainDiagnostic, StoppedDrainDiagnostic, UnavailableDrainDiagnostic,
)
from agent_comms.cohort_schema import CohortSchemaMeta
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.drain_failures import drain_failures
from test_acp_private_nk_delivery import _session
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture
pytestmark = pytest.mark.usefixtures("fake_native_catalog")


async def until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.02)


def owner_view(comms):
    return next(
        view for view in Comms(comms.root).views.thread_views() if view.thread.name == "beta"
    )


@pytest.mark.parametrize("broken", ["missing", "drifted"])
async def test_live_drain_schema_failure_visible_until_real_recovery(tmp_path, broken):
    comms, agent, _ = _session(tmp_path)
    path = comms.root / "coordination.sqlite3"
    saved = tmp_path / "current-schema.sqlite3"
    shutil.copy2(path, saved)
    if broken == "missing":
        path.unlink()
        with Coordination(path):
            pass
    else:
        with sqlite3.connect(path) as db:
            db.execute(f'DROP TABLE "{CohortSchemaMeta.declared_name}"')
    try:
        agent.inputs.ensure_live_drain("beta")
        await until(lambda: agent.inputs.state("beta").observation_watcher is not None)
        agent.inputs.wake_observer("beta")
        await until(
            lambda: owner_view(comms).activity.readiness.source_diagnostic() is not None
        )
        failed = owner_view(comms)
        diagnostic = failed.activity.readiness.source_diagnostic()
        assert isinstance(diagnostic, UnavailableDrainDiagnostic)
        assert failed.presentation.status.details == (diagnostic.summary,)
        assert not failed.presentation.status.working
        assert "Inbox unavailable" in failed.presentation.summary
        assert diagnostic.error_type in failed.presentation.summary
        assert "Ready" not in failed.presentation.summary
        documents = list((comms.root / "diagnostics").glob("drain-*.json"))
        assert documents
        encoded_diagnostics = [
            json.loads(document.read_text())["drain"] for document in documents
        ]
        assert all("recorded_at" not in encoded for encoded in encoded_diagnostics)
        assert all(encoded["kind"] == "unavailable_drain" for encoded in encoded_diagnostics)
        # Without a real dependency change, failure neither retries nor repairs the schema.
        log = comms.root / "activity.jsonl"
        observed = log.read_bytes()
        await asyncio.sleep(1.3)
        assert log.read_bytes() == observed
        with sqlite3.connect(path) as db:
            assert (
                db.execute(
                    "SELECT name FROM sqlite_master WHERE name=?", (CohortSchemaMeta.declared_name,)
                ).fetchone()
                is None
            )
        # Age and an unrelated turn may not turn this persistent failure into Ready.
        comms.agents.activity.emit(
            replace(failed.activity.source_event(), timestamp=time.time() - 1000)
        )
        assert owner_view(comms).presentation.status.details == (diagnostic.summary,)
        lease = comms.agents.begin_turn("beta", "ordinary-unrelated-turn").turn_lease
        comms.agents.finish_turn(lease)
        assert owner_view(comms).presentation.status.details == (diagnostic.summary,)
        # An explicit operator restores the actual current schema; readers do no repair.
        os.replace(saved, path)
        await until(
            lambda: owner_view(comms).activity.readiness.source_diagnostic() is None
        )
        assert owner_view(comms).presentation.summary == "Ready"
        assert not owner_view(comms).presentation.status.details
        recovered = log.read_bytes()
        await asyncio.sleep(1.3)
        assert log.read_bytes() == recovered
    finally:
        await agent.shutdown()


async def test_successful_drain_clears_failure_during_active_turn(tmp_path):
    comms, agent, _ = _session(tmp_path)
    owner = comms.registry.snapshot().owner_identity("beta")

    with pytest.raises(OSError, match="past drain failed"):
        async with drain_failures(comms, "beta", owner):
            raise OSError("past drain failed")
    lease = comms.agents.begin_turn("beta", "later-successful-turn").turn_lease
    try:
        async with drain_failures(comms, "beta", owner):
            await agent.inputs.drain_inbox("beta")
        current = owner_view(comms)
        assert current.activity.readiness.source_diagnostic() is None
        assert current.thread.turn_record.read().executing
        assert current.presentation.status.working
        assert not current.presentation.status.details
        activity = (comms.root / "activity.jsonl").read_bytes()
        async with drain_failures(comms, "beta", owner):
            await agent.inputs.drain_inbox("beta")
        assert (comms.root / "activity.jsonl").read_bytes() == activity
    finally:
        comms.agents.finish_turn(lease)
        await agent.shutdown()


@pytest.mark.parametrize("kind", [
    UnavailableDrainDiagnostic, StoppedDrainDiagnostic, NativeInputDrainDiagnostic,
])
async def test_restarted_observer_recovers_retained_diagnostic(tmp_path, kind):
    comms, agent, _ = _session(tmp_path)
    owner = comms.registry.snapshot().owner_identity("beta")
    # A different producer recorded this before the observer existed. There is
    # no process-local failure state for the new observer to inherit.
    comms.agents.set_drain_diagnostic(
        "beta", owner, kind(owner, "OSError", "previous observation failed", recorded_at=1.0),
    )
    assert owner_view(comms).activity.readiness.source_diagnostic() is not None
    try:
        agent.inputs.ensure_live_drain("beta")
        await until(lambda: owner_view(comms).activity.readiness.source_diagnostic() is None)
        assert not owner_view(comms).presentation.status.details
    finally:
        await agent.shutdown()


async def test_settled_observer_recovers_another_path_failure(tmp_path):
    comms, agent, _ = _session(tmp_path)
    owner = comms.registry.snapshot().owner_identity("beta")
    try:
        agent.inputs.ensure_live_drain("beta")
        await until(lambda: agent.inputs._quiescent("beta"))
        diagnostic = UnavailableDrainDiagnostic(
            owner, "OSError", "selected wake failed", recorded_at=1.0,
        )
        comms.agents.set_drain_diagnostic("beta", owner, diagnostic)
        assert owner_view(comms).activity.readiness.source_diagnostic() == diagnostic
        # This is the existing failed-wake notification. Unchanged input stores
        # must not make a settled observer skip the actual recovery read.
        agent.inputs.wake_observer("beta")
        await until(lambda: owner_view(comms).activity.readiness.source_diagnostic() is None)
        assert not owner_view(comms).presentation.status.details
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("recurs", [False, True])
async def test_successful_earlier_drain_preserves_newer_error(tmp_path, recurs):
    comms, agent, _ = _session(tmp_path)
    owner = comms.registry.snapshot().owner_identity("beta")
    first = UnavailableDrainDiagnostic(owner, "OSError", "first failure", recorded_at=1.0)
    comms.agents.set_drain_diagnostic("beta", owner, first)

    try:
        async with drain_failures(comms, "beta", owner):
            await agent.inputs.drain_inbox("beta")
            newer = replace(first, reason="different failure", recorded_at=2.0)
            comms.agents.set_drain_diagnostic("beta", owner, newer)
            if recurs:
                # Equality deduplicates repeated retries, but A->B->A is a new
                # observation even when its ordinary diagnostic fields match A.
                newer = replace(first, recorded_at=3.0)
                comms.agents.set_drain_diagnostic("beta", owner, newer)
        retained = owner_view(comms).activity.readiness.source_diagnostic()
        assert retained == newer
        assert retained.recorded_at == newer.recorded_at
        async with drain_failures(comms, "beta", owner):
            await agent.inputs.drain_inbox("beta")
        assert owner_view(comms).activity.readiness.source_diagnostic() is None
    finally:
        await agent.shutdown()


async def test_predecessor_success_cannot_clear_replacement_owner_failure(tmp_path):
    comms, agent, _ = _session(tmp_path)
    owner = comms.registry.snapshot().owner_identity("beta")
    first = UnavailableDrainDiagnostic(owner, "OSError", "first failure", recorded_at=1.0)
    comms.agents.set_drain_diagnostic("beta", owner, first)

    try:
        async with drain_failures(comms, "beta", owner):
            await agent.inputs.drain_inbox("beta")
            comms.registry.register(comms.registry.require("beta"), new_owner=True)
            successor = comms.registry.snapshot().owner_identity("beta")
            assert successor != owner
            newer = replace(first, owner=successor, recorded_at=2.0)
            comms.agents.set_drain_diagnostic("beta", successor, newer)
        assert owner_view(comms).activity.readiness.source_diagnostic() == newer
    finally:
        await agent.shutdown()


async def test_cancelled_drain_keeps_original_failure(tmp_path):
    comms, agent, _ = _session(tmp_path)
    owner = comms.registry.snapshot().owner_identity("beta")
    first = UnavailableDrainDiagnostic(owner, "OSError", "first failure", recorded_at=1.0)
    comms.agents.set_drain_diagnostic("beta", owner, first)
    log = (comms.root / "activity.jsonl").read_bytes()

    try:
        with pytest.raises(asyncio.CancelledError):
            async with drain_failures(comms, "beta", owner):
                raise asyncio.CancelledError()
        assert (comms.root / "activity.jsonl").read_bytes() == log
        assert owner_view(comms).activity.readiness.source_diagnostic() == first
    finally:
        await agent.shutdown()


async def test_unexpected_drain_failure_is_visible_and_propagates(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)

    async def broken(_session):
        raise ValueError("unexpected drain implementation failure")

    monkeypatch.setattr(agent.inputs, "drain_inbox", broken)
    try:
        # Invoke the real retirement callback explicitly so its process exit
        # does not terminate pytest's own event loop.
        task = asyncio.create_task(agent.inputs.observe("beta"))
        agent.inputs.state("beta").drain_task = task
        with pytest.raises(ValueError, match="unexpected drain implementation failure"):
            await task
        with pytest.raises(SystemExit) as stopped:
            agent.inputs._retire_observer("beta", task)
        assert stopped.value.code == 1
        assert isinstance(
            owner_view(comms).activity.readiness.source_diagnostic(),
            StoppedDrainDiagnostic,
        )
        assert "Drain stopped" in owner_view(comms).presentation.summary
        # A replacement owner cannot inherit the predecessor's failure.
        thread = comms.registry.require("beta")
        comms.registry.register(thread, new_owner=True)
        assert owner_view(comms).activity.readiness.source_diagnostic() is None
    finally:
        await agent.shutdown()
