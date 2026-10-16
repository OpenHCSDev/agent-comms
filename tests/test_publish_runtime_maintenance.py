"""The installed runtime entrypoint uses the root's durable maintenance phase."""

import asyncio
import importlib
import os
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from agent_comms import cohort_foreground, native_pi
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.coordination_cohort import next_sealed_assignment
from agent_comms.coordinator import Coordination
from agent_comms.maintenance_barrier import DrainingPhase, ReadyPhase
from agent_comms.schedule_rules import SelectedWakeCheck
from test_acp_private_nk_delivery import _session


def _publisher(tmp_path, monkeypatch, comms, publish, route_read):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'tools/cutover'))
    entry = importlib.import_module('publish_runtime')
    target = tmp_path / 'target-runtime'
    (target / 'bin').mkdir(parents=True)
    publisher = SimpleNamespace(runtime=target, publish=publish)
    monkeypatch.setattr(entry.RuntimePublication, 'installed', classmethod(lambda cls: publisher))
    monkeypatch.setattr(entry, 'read_active_route', route_read)
    monkeypatch.setattr(entry, 'wire', lambda route: comms)
    requests = []

    def enqueue(comms, name):
        requests.append(name)
        return SimpleNamespace(id='scratch', state=SimpleNamespace(declared_name='pending'))

    monkeypatch.setattr(entry, 'enqueue', enqueue)
    monkeypatch.setattr(entry.sys, 'argv', ['publish_runtime.py'])
    return entry, requests


def test_runtime_pointer_replacement_syncs_its_directory(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'tools/cutover'))
    entry = importlib.import_module('publish_runtime')
    target = tmp_path / 'runtime'
    target.mkdir()
    link = tmp_path / 'current'
    sync = entry.os.fsync
    synced = []

    def observe(directory):
        synced.append(os.fstat(directory).st_ino)
        return sync(directory)

    with monkeypatch.context() as recorder:
        recorder.setattr(entry.os, 'fsync', observe)
        entry.RuntimePublication.replace_link(link, target)
    assert link.resolve(strict=True) == target
    assert synced == [tmp_path.stat().st_ino]


@pytest.mark.asyncio
async def test_mid_publication_channel_post_runs_after_clear(tmp_path, monkeypatch):
    comms, agent, _root_id = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, '_trusted_package', lambda _: None)
    monkeypatch.setattr(native_pi, '_trusted_package', lambda _: None)
    gate = comms.owners.maintenance
    entered, release = threading.Event(), threading.Event()

    def publish():
        assert gate.read().phase is DrainingPhase
        entered.set()
        assert release.wait(5), 'scratch publication was not released'

    route = object()
    entry, requests = _publisher(tmp_path, monkeypatch, comms, publish, lambda: route)
    original_path = os.environ['PATH']
    original_venv = os.environ.get('VIRTUAL_ENV')
    task = asyncio.create_task(asyncio.to_thread(entry.main))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        post = comms.messaging.send_message('sender', '#team', 'during pointer publication')
        assert await agent.inputs.drain_inbox('beta') == 0
        assert await agent.inputs.drain_inbox('beta') == 0
        beta = comms.registry.require('beta')
        with Coordination(comms.root / 'coordination.sqlite3') as store:
            assert next_sealed_assignment(store, stable_thread_lookup(beta.created_at), beta.name).wire_seq == post.seq
        assert beta.active_turn is None
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
        os.environ['PATH'] = original_path
        if original_venv is None:
            os.environ.pop('VIRTUAL_ENV', None)
        else:
            os.environ['VIRTUAL_ENV'] = original_venv
    assert gate.read().phase is ReadyPhase
    assert requests  # Existing idle-reload queue runs only after reopening.
    scheduled = []
    monkeypatch.setattr(SelectedWakeCheck, 'schedule', lambda check: scheduled.append(check))
    assert await agent.inputs.drain_inbox('beta') == 1
    assert len(scheduled) == 1


@pytest.mark.asyncio
async def test_partial_publication_failure_does_not_reopen(tmp_path, monkeypatch):
    comms, _agent, _root_id = _session(tmp_path)
    gate = comms.owners.maintenance
    touched = tmp_path / 'pointer-touched'

    def publish():
        touched.write_text('possible target publication')
        raise OSError('publication outcome unknown')

    route = object()
    entry, requests = _publisher(tmp_path, monkeypatch, comms, publish, lambda: route)
    with pytest.raises(OSError, match='outcome unknown'):
        await asyncio.to_thread(entry.main)
    assert touched.is_file() and not requests
    assert gate.read().phase is DrainingPhase


@pytest.mark.asyncio
async def test_prepublication_abort_clears_phase_and_delivers_post(tmp_path, monkeypatch):
    comms, agent, _root_id = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, '_trusted_package', lambda _: None)
    monkeypatch.setattr(native_pi, '_trusted_package', lambda _: None)
    gate = comms.owners.maintenance
    route = object()
    calls, posted = [], []

    def read_route():
        calls.append(None)
        if len(calls) == 2:
            assert gate.read().phase is DrainingPhase
            posted.append(comms.messaging.send_message('sender', '#team', 'during aborted cutover'))
            return object()  # Abort before any pointer mutation.
        return route

    def never_publish():
        pytest.fail('A changed route must abort before publishing')

    entry, requests = _publisher(tmp_path, monkeypatch, comms, never_publish, read_route)
    with pytest.raises(ValueError, match='Active route changed'):
        await asyncio.to_thread(entry.main)
    assert not requests and len(posted) == 1
    assert gate.read().phase is ReadyPhase
    selected = []
    monkeypatch.setattr(SelectedWakeCheck, 'schedule', lambda check: selected.append(check))
    assert await agent.inputs.drain_inbox('beta') == 1
    assert len(selected) == 1
