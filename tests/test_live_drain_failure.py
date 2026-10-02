"""The actual background drain exposes broken SQLite state and observes recovery."""

import asyncio
import os
import shutil
import sqlite3
import time
from dataclasses import replace

import pytest

from agent_comms.activity import StoppedDrainDiagnostic, UnavailableDrainDiagnostic
from agent_comms.cohort_schema import CohortSchemaMeta
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from test_acp_private_nk_delivery import _session
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


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
        await until(lambda: owner_view(comms).activity.diagnostic is not None)
        failed = owner_view(comms)
        assert isinstance(failed.activity.diagnostic, UnavailableDrainDiagnostic)
        assert failed.presentation.attention and not failed.presentation.busy
        assert "Inbox unavailable" in failed.presentation.summary
        assert failed.activity.diagnostic.error_type in failed.presentation.summary
        assert "Ready" not in failed.presentation.summary
        # Repeated retries must neither flood the log nor repair the broken schema.
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
        comms.agents.activity.emit(replace(failed.activity, timestamp=time.time() - 1000))
        assert owner_view(comms).presentation.attention
        lease = comms.agents.begin_turn("beta", "ordinary-unrelated-turn").turn_lease
        comms.agents.finish_turn(lease)
        assert owner_view(comms).presentation.attention
        # An explicit operator restores the actual current schema; readers do no repair.
        os.replace(saved, path)
        await until(lambda: owner_view(comms).activity.diagnostic is None)
        assert owner_view(comms).presentation.summary == "Ready"
        assert not owner_view(comms).presentation.attention
        recovered = log.read_bytes()
        await asyncio.sleep(1.3)
        assert log.read_bytes() == recovered
    finally:
        await agent.shutdown()


async def test_unexpected_drain_failure_is_visible_and_propagates(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)

    async def broken(_session):
        raise TypeError("unexpected drain implementation failure")

    monkeypatch.setattr(agent.inputs, "drain_inbox", broken)
    try:
        agent.inputs.ensure_live_drain("beta")
        task = agent.inputs.drain_tasks["beta"]
        with pytest.raises(TypeError, match="unexpected drain implementation failure"):
            await task
        assert isinstance(owner_view(comms).activity.diagnostic, StoppedDrainDiagnostic)
        assert "Drain stopped" in owner_view(comms).presentation.summary
        # A replacement owner cannot inherit the predecessor's failure.
        thread = comms.registry.require("beta")
        comms.registry.register(thread, new_owner=True)
        assert owner_view(comms).activity.diagnostic is None
    finally:
        await agent.shutdown()
