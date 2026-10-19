"""Presence behavior across persisted lifecycle changes and current UI consumers."""

import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from agent_comms.activity import Activity, ActivityState
from agent_comms.comms import wire
from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.field_codec import FieldCodec
from agent_comms.presentation import ThreadView
from agent_comms.registration import Registration
from agent_comms.thread_status import (
    ArchivedThreadStatus,
    DeletingThreadStatus,
    IdleThreadStatus,
    RunningThreadStatus,
    StoppedThreadStatus,
    ThreadStatus,
)
from agent_comms.threads import Thread


def test_captured_owner_presentation_survives_exit_until_fresh_acquisition(tmp_path):
    comms = wire(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        thread = comms.registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(child.pid))
        comms.registry.register(thread, RunningThreadStatus())
        view, = comms.views.thread_views()
        displayed = view.presentation
        # Cross the same original codec boundary used by worker-prepared views.
        transferred = FieldCodec.decode(ThreadView, FieldCodec.encode(view))
        child.terminate()
        child.wait(timeout=5)
        assert not thread.process_alive
        assert view.presentation == displayed
        assert transferred.presentation == displayed
        fresh, = comms.views.thread_views()
        assert fresh.presentation.summary == "Owner exited"
        assert not fresh.presentation.status.working
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize("status", [member() for member in ThreadStatus.members_with(ThreadStatus)])
def test_saved_presence_roundtrip_and_wire_views_preserve_data(tmp_path, status):
    comms = wire(tmp_path)
    thread = comms.registry.new_thread('owner', frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), title='Retained title', session_file='/retained/session.jsonl', created_at=12.5)
    comms.registry.register(thread, status)
    before = comms.registry.snapshot()
    raw = json.loads(comms.registry.store.path.read_text())
    assert FieldCodec.decode(ThreadStatus, raw["statuses"]["owner"]) == status
    assert raw["threads"]["owner"]["created_at"] == 12.5
    reopened = Registration(comms.registry.store.path)
    assert reopened.snapshot() == before
    assert reopened.status("owner") == status
    assert FieldCodec.decode(ThreadStatus, FieldCodec.encode(status)) == status
    assert comms.views.thread_detail("owner").encoded()["status"] == status.declared_name
    assert comms.views.list_threads()[0].status.declared_name == status.declared_name
    activity = Activity("owner", ActivityState.WORKING, "stale or current activity")
    comms.agents.activity.emit(activity)
    visible = comms.views.thread_views()
    individual = comms.views.thread_presentations(("owner",)).read("owner")
    if visible:
        # Opening the individual acquires a transcript reader; the roster owns
        # none. Its original identity must bind this owner, while presentation
        # of the common lifecycle/readiness is identical.
        assert individual.read_identity.thread.incarnation == thread.incarnation
        assert replace(individual, read_identity=None) == visible[0].presentation
    else:
        assert individual is None
    assert comms.views.thread_presentations(("missing",)).read("missing") is None


@pytest.mark.parametrize("status", [RunningThreadStatus(), IdleThreadStatus()])
def test_live_owner_cannot_be_deleted_and_heartbeat_keeps_both_counters(tmp_path, status):
    registry = Registration(tmp_path / "registry.json")
    registry.register(
        registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())),
        status,
    )
    before = registry.snapshot()
    saved = registry.store.path.read_bytes()
    with pytest.raises(RelationViolationError, match="Stop a running thread"):
        registry.begin_delete("owner")
    assert registry.store.path.read_bytes() == saved
    registry.heartbeat("owner")
    assert registry.snapshot().owner_generations == before.owner_generations
    assert registry.snapshot().claimed_generations == before.claimed_generations
    assert registry.status("owner").running


@pytest.mark.parametrize("status", [StoppedThreadStatus(), ArchivedThreadStatus()])
def test_reactivation_rotates_owner_and_claim_without_rebinding_history(tmp_path, status):
    registry = Registration(tmp_path / "registry.json")
    registry.register(
        registry.new_thread('owner', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())),
        status,
    )
    before = registry.snapshot()
    registry.heartbeat("owner")
    after = registry.snapshot()
    assert after.threads["owner"].incarnation == before.threads["owner"].incarnation
    assert after.owner_generations["owner"] > before.owner_generations["owner"]
    assert after.claimed_generations["owner"] > before.claimed_generations["owner"]
    assert not after.threads["owner"].active_turn.executing


def test_delete_fence_rejects_metadata_and_presence_without_erasing_data(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    registry.register(registry.new_thread('owner', frozenset(), str(tmp_path)), StoppedThreadStatus())
    registry.begin_delete("owner")
    before = registry.store.path.read_bytes()
    with pytest.raises(RelationViolationError, match="permanently deleted"):
        registry.heartbeat("owner")
    with pytest.raises(RelationViolationError, match="permanently deleted"):
        registry.register(replace(registry.require("owner"), title="late metadata"))
    assert registry.store.path.read_bytes() == before
    assert registry.status("owner") == DeletingThreadStatus()


def test_roster_and_controls_keep_archived_threads_dormant():
    from agent_comms.tools import CommsStartTool, ToolRequest
    from agent_comms.thread_execution import OwnerLifecycleControl
    from agent_comms.child_process import NoProcess

    archived = ArchivedThreadStatus()
    assert archived.restored() == archived
    for command in ToolRequest.members_with(OwnerLifecycleControl):
        assert not command.available_for(archived, process=ProcessIdentity(123, 1))
    stopped = StoppedThreadStatus()
    assert CommsStartTool.available_for(stopped, process=ProcessIdentity(123, 1))
    for live in (RunningThreadStatus(), IdleThreadStatus()):
        assert live.restored() == stopped
        assert not CommsStartTool.available_for(live, process=ProcessIdentity(123, 1))
        assert CommsStartTool.available_for(live, process=NoProcess())


def test_batch_source_rename_keeps_the_original_registry_fence(comms):
    from agent_comms.coordination_errors import StaleRevision

    comms.registry.register(
        comms.registry.new_thread('alpha', frozenset(), str(comms.root), process_identity=ProcessIdentity.capture(os.getpid())),
        RunningThreadStatus(),
    )
    batch = comms.views.thread_presentations(("alpha",))
    comms.registry.rename("alpha", "renamed")
    with pytest.raises(StaleRevision, match="incarnation changed"):
        batch.read("alpha")
    current = comms.views.thread_presentations(("alpha",))
    assert set(current.views) == {"renamed"}
    assert current.require("alpha").read_identity.thread.incarnation == comms.registry.require("renamed").incarnation


def test_required_presentation_rejects_an_unavailable_name(comms):
    batch = comms.views.thread_presentations(("missing",))
    assert batch.read("missing") is None
    with pytest.raises(UnregisteredThreadError, match="Thread presentation unavailable: missing"):
        batch.require("missing")
    assert not hasattr(comms.views, "thread_presentation")
    assert not hasattr(comms.views, "require_thread_presentation")
