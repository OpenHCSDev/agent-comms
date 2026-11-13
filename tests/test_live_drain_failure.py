"""The actual background drain exposes broken SQLite state and observes recovery."""


import asyncio
import json
import os
import shutil
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.execution.acp import CommsAgent
from agent_comms.comms import Comms, wire
from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.drain_failures import drain_failures
from agent_comms.installer.native_package import VerifiedNativePackage
from agent_comms.nominal.field_codec import AcpSessionId, TurnId
from agent_comms.registry.activity import (
    NativeInputDrainDiagnostic,
    StoppedDrainDiagnostic,
    UnavailableDrainDiagnostic,
)
from agent_comms.team.standing import InboxUnavailable, UnitDeaf
from native_backend_fixture import wait_for as until
from test_acp_private_nk_delivery import _private_comms, _session
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


@pytest.fixture
async def owned_session(
    tmp_path: Path, native_runtime_package: VerifiedNativePackage,
) -> tuple[Comms, CommsAgent, AcpSessionId]:
    """A published native session with its real package and allocated drain owner."""
    comms, agent, _, session_id = _session(tmp_path)
    agent.inputs.acquire_owner(
        session_id, agent.inputs.configuration,
        comms.registry.snapshot().owner_identity("beta"),
    )
    return comms, agent, session_id


def owner_view(comms):
    return next(
        view for view in wire(comms.root).views.thread_views() if view.thread.name == "beta"
    )


@pytest.mark.parametrize("broken", ["missing", "drifted"])
async def test_live_drain_schema_failure_visible_until_real_recovery(tmp_path, broken, owned_session):
    comms, agent, *_ = owned_session
    path = comms.root / "coordination.sqlite3"
    saved = tmp_path / "current-schema.sqlite3"
    shutil.copy2(path, saved)
    if broken == "missing":
        path.unlink()
        with Coordination(path):
            pass
    else:
        with sqlite3.connect(path) as db:
            db.execute("DROP TABLE claim_batch_members")
    with sqlite3.connect(path) as db:
        broken_schema = db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    try:
        agent.inputs.ensure_live_drain(AcpSessionId("beta"))
        await until(lambda: agent.inputs.state(AcpSessionId("beta")).observation_watcher is not None)
        await until(
            lambda: owner_view(comms).activity.readiness.source_diagnostic().holds_event_current
        )
        failed = owner_view(comms)
        diagnostic = failed.activity.readiness.source_diagnostic()
        assert isinstance(diagnostic, UnavailableDrainDiagnostic)
        assert failed.presentation.status.details == (diagnostic.summary,)
        assert not failed.presentation.status.is_working()
        documents = list((comms.root / "diagnostics").glob("drain-*.json"))
        assert documents
        encoded_diagnostics = [
            json.loads(document.read_text())["drain"] for document in documents
        ]
        assert all("recorded_at" not in encoded for encoded in encoded_diagnostics)
        assert all(encoded["kind"] == "unavailable_drain" for encoded in encoded_diagnostics)
        # Without a real dependency change, failure neither retries nor repairs the schema.
        observed = comms.agents.activity.current("beta", active=True)
        await asyncio.sleep(1.3)
        assert comms.agents.activity.current("beta", active=True) == observed
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == broken_schema
        # Age and an unrelated turn may not turn this persistent failure into Ready.
        comms.agents.activity.emit(
            replace(failed.activity.source_event(), timestamp=time.time() - 1000)
        )
        assert owner_view(comms).presentation.status.details == (diagnostic.summary,)
        lease = comms.agents.begin_turn(
            "beta", TurnId('8cd49e85997e272cc5c54e3ea84e658d')
        ).turn_lease
        comms.agents.finish_turn(lease)
        assert owner_view(comms).presentation.status.details == (diagnostic.summary,)
        # An explicit operator restores the actual current schema; readers do no repair.
        os.replace(saved, path)
        await until(
            lambda: not owner_view(comms).activity.readiness.source_diagnostic().holds_event_current
        )
        assert not owner_view(comms).presentation.status.details
        recovered = comms.agents.activity.current("beta", active=True)
        await asyncio.sleep(1.3)
        assert comms.agents.activity.current("beta", active=True) == recovered
    finally:
        await agent.shutdown()


async def test_successful_drain_clears_failure_during_active_turn(tmp_path, owned_session):
    comms, agent, *_ = owned_session
    owner = comms.registry.snapshot().owner_identity("beta")

    with pytest.raises(OSError):
        async with drain_failures(comms, "beta", owner):
            raise OSError("past drain failed")
    lease = comms.agents.begin_turn("beta", TurnId('028a74a690b644b1557f0f9b70b9d9a2')).turn_lease
    try:
        async with drain_failures(comms, "beta", owner):
            await agent.inputs.drain_inbox(AcpSessionId("beta"))
        current = owner_view(comms)
        assert not current.activity.readiness.source_diagnostic().holds_event_current
        assert current.thread.executing
        assert current.presentation.status.is_working()
        assert not current.presentation.status.details
        activity = comms.agents.activity.current("beta", active=True)
        async with drain_failures(comms, "beta", owner):
            await agent.inputs.drain_inbox(AcpSessionId("beta"))
        assert comms.agents.activity.current("beta", active=True) == activity
    finally:
        comms.agents.finish_turn(lease)
        await agent.shutdown()


@pytest.mark.parametrize("kind", [
    UnavailableDrainDiagnostic, StoppedDrainDiagnostic, NativeInputDrainDiagnostic,
])
async def test_restarted_observer_recovers_retained_diagnostic(tmp_path, kind, owned_session):
    comms, agent, *_ = owned_session
    owner = comms.registry.snapshot().owner_identity("beta")
    # A different producer recorded this before the observer existed. There is
    # no process-local failure state for the new observer to inherit.
    comms.agents.set_drain_diagnostic(
        "beta", owner, kind(owner, "OSError", "previous observation failed", recorded_at=1.0),
    )
    assert owner_view(comms).activity.readiness.source_diagnostic().holds_event_current
    try:
        agent.inputs.ensure_live_drain(AcpSessionId("beta"))
        await until(lambda: not owner_view(comms).activity.readiness.source_diagnostic().holds_event_current)
        assert not owner_view(comms).presentation.status.details
        await agent.inputs.state(AcpSessionId("beta")).observer.stop()
        # A new consumer gets its first read even while the shared root is idle.
        agent.inputs.ensure_live_drain(AcpSessionId("beta"))
        await until(lambda: agent.inputs.state(AcpSessionId("beta")).observation_watcher is not None)
        await until(lambda: agent.inputs._quiescent(AcpSessionId("beta")))
    finally:
        await agent.shutdown()


async def test_settled_observer_recovers_another_path_failure(tmp_path, owned_session):
    comms, agent, *_ = owned_session
    owner = comms.registry.snapshot().owner_identity("beta")
    try:
        agent.inputs.ensure_live_drain(AcpSessionId("beta"))
        await until(lambda: agent.inputs._quiescent(AcpSessionId("beta")))
        diagnostic = UnavailableDrainDiagnostic(
            owner, "OSError", "selected wake failed", recorded_at=1.0,
        )
        comms.agents.set_drain_diagnostic("beta", owner, diagnostic)
        assert owner_view(comms).activity.readiness.source_diagnostic() == diagnostic
        # This is the existing failed-wake notification. Unchanged input stores
        # must not make a settled observer skip the actual recovery read.
        agent.inputs.wake_observer(AcpSessionId("beta"))
        await until(lambda: not owner_view(comms).activity.readiness.source_diagnostic().holds_event_current)
        assert not owner_view(comms).presentation.status.details
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("recurs", [False, True])
async def test_successful_earlier_drain_preserves_newer_error(tmp_path, recurs, owned_session):
    comms, agent, *_ = owned_session
    owner = comms.registry.snapshot().owner_identity("beta")
    first = UnavailableDrainDiagnostic(owner, "OSError", "first failure", recorded_at=1.0)
    comms.agents.set_drain_diagnostic("beta", owner, first)

    try:
        async with drain_failures(comms, "beta", owner):
            await agent.inputs.drain_inbox(AcpSessionId("beta"))
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
            await agent.inputs.drain_inbox(AcpSessionId("beta"))
        assert not owner_view(comms).activity.readiness.source_diagnostic().holds_event_current
    finally:
        await agent.shutdown()


async def test_predecessor_success_cannot_clear_replacement_owner_failure(tmp_path, owned_session):
    comms, agent, *_ = owned_session
    owner = comms.registry.snapshot().owner_identity("beta")
    first = UnavailableDrainDiagnostic(owner, "OSError", "first failure", recorded_at=1.0)
    comms.agents.set_drain_diagnostic("beta", owner, first)

    try:
        async with drain_failures(comms, "beta", owner):
            await agent.inputs.drain_inbox(AcpSessionId("beta"))
            comms.registry.register(comms.registry.require("beta"), new_owner=True)
            successor = comms.registry.snapshot().owner_identity("beta")
            assert successor != owner
            newer = replace(first, owner=successor, recorded_at=2.0)
            comms.agents.set_drain_diagnostic("beta", successor, newer)
        assert owner_view(comms).activity.readiness.source_diagnostic() == newer
    finally:
        await agent.shutdown()


async def test_cancelled_drain_keeps_original_failure(tmp_path, owned_session):
    comms, agent, *_ = owned_session
    owner = comms.registry.snapshot().owner_identity("beta")
    first = UnavailableDrainDiagnostic(owner, "OSError", "first failure", recorded_at=1.0)
    comms.agents.set_drain_diagnostic("beta", owner, first)
    activity = comms.agents.activity.current("beta", active=True)

    try:
        with pytest.raises(asyncio.CancelledError):
            async with drain_failures(comms, "beta", owner):
                raise asyncio.CancelledError()
        assert comms.agents.activity.current("beta", active=True) == activity
        assert owner_view(comms).activity.readiness.source_diagnostic() == first
    finally:
        await agent.shutdown()




@pytest.mark.parametrize("kind, deaf", [
    (UnavailableDrainDiagnostic, True), (StoppedDrainDiagnostic, True),
    (NativeInputDrainDiagnostic, False),
])
def test_deaf_teammate_is_dated_by_the_drain_that_cannot_read_its_inbox(
    tmp_path, monkeypatch, kind, deaf,
):

    comms, _ = _private_comms(tmp_path)
    owner = comms.registry.snapshot().owner_identity("beta")
    assert UnitDeaf.violations(comms) == ()
    diagnostic = kind(owner, "OSError", "inbox read failed", recorded_at=100.0)
    comms.agents.set_drain_diagnostic("beta", owner, diagnostic)
    monkeypatch.setattr(time, "time", lambda: 100.0 + InboxUnavailable.seconds - 1)
    assert UnitDeaf.violations(comms) == ()
    monkeypatch.setattr(time, "time", lambda: 100.0 + InboxUnavailable.seconds + 1)
    found = UnitDeaf.violations(comms)
    beta = comms.registry.require("beta").incarnation.require_recorded()
    assert found == ((UnitDeaf(100.0 + InboxUnavailable.seconds, beta),) if deaf else ())
    if deaf:
        assert found[0].broke_after(100.0)
