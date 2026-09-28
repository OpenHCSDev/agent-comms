"""Presence behavior across persisted lifecycle changes and current UI consumers."""

import json
import os
from dataclasses import replace

import pytest

from agent_comms import wire
from agent_comms.activity import Activity, ActivityState
from agent_comms.errors import RelationViolationError
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


@pytest.mark.parametrize("status", [member() for member in ThreadStatus.members_with(ThreadStatus)])
def test_saved_presence_roundtrip_and_wire_views_preserve_data(tmp_path, status):
    comms = wire(tmp_path)
    thread = Thread(
        "owner",
        frozenset({"team"}),
        str(tmp_path),
        pid=os.getpid(),
        title="Retained title",
        session_file="/retained/session.jsonl",
        created_at=12.5,
    )
    comms.registry.register(thread, status)
    before = comms.registry.snapshot()
    raw = json.loads(comms.registry.store.path.read_text())
    assert raw["threads"]["owner"]["status"] == status.declared_name
    assert raw["threads"]["owner"]["created_at"] == 12.5
    reopened = Registration(comms.registry.store.path)
    assert reopened.snapshot() == before
    assert reopened.status("owner") == status
    assert FieldCodec.decode(ThreadStatus, FieldCodec.encode(status)) == status
    assert comms.thread_detail("owner")["status"] == status.declared_name
    assert comms.list_threads()[0]["status"] == status.declared_name
    activity = Activity("owner", ActivityState.WORKING, "stale or current activity")
    view = ThreadView(thread, reopened.status("owner"), activity, None, 0)
    assert view.to_wire()["status"] == status.declared_name
    assert view.presentation.busy == status.active


@pytest.mark.parametrize("status", [RunningThreadStatus(), IdleThreadStatus()])
def test_live_owner_cannot_be_deleted_and_heartbeat_keeps_both_counters(tmp_path, status):
    registry = Registration(tmp_path / "registry.json")
    registry.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()), status)
    before = registry.snapshot()
    saved = registry.store.path.read_bytes()
    with pytest.raises(RelationViolationError, match="Stop a running thread"):
        registry.begin_delete("owner")
    assert registry.store.path.read_bytes() == saved
    registry.heartbeat("owner")
    assert registry.snapshot().owner_generations == before.owner_generations
    assert registry.snapshot().admission_generations == before.admission_generations
    assert registry.status("owner").running


@pytest.mark.parametrize("status", [StoppedThreadStatus(), ArchivedThreadStatus()])
def test_reactivation_rotates_owner_and_admission_without_rebinding_history(tmp_path, status):
    registry = Registration(tmp_path / "registry.json")
    registry.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()), status)
    before = registry.snapshot()
    registry.heartbeat("owner")
    after = registry.snapshot()
    assert after.threads["owner"].incarnation == before.threads["owner"].incarnation
    assert after.owner_generations["owner"] > before.owner_generations["owner"]
    assert after.admission_generations["owner"] > before.admission_generations["owner"]
    assert after.threads["owner"].active_turn is None


def test_delete_fence_rejects_metadata_and_presence_without_erasing_data(tmp_path):
    registry = Registration(tmp_path / "registry.json")
    registry.register(Thread("owner", frozenset(), str(tmp_path)), StoppedThreadStatus())
    registry.begin_delete("owner")
    before = registry.store.path.read_bytes()
    with pytest.raises(RelationViolationError, match="permanently deleted"):
        registry.heartbeat("owner")
    with pytest.raises(RelationViolationError, match="permanently deleted"):
        registry.register(replace(registry.require("owner"), title="late metadata"))
    assert registry.store.path.read_bytes() == before
    assert registry.status("owner") == DeletingThreadStatus()


def test_roster_and_controls_keep_archived_threads_dormant():
    archived = ArchivedThreadStatus()
    assert archived.restored() == archived
    assert not archived.in_view()
    assert archived.in_view(show_archived=True)
    assert not archived.allows_control("comms_start", owner_pid=123)
    assert not archived.allows_control("comms_stop", owner_pid=123)
    assert not archived.allows_control("comms_archive", owner_pid=123)
    assert archived.allows_control("comms_delete", owner_pid=123)
    stopped = StoppedThreadStatus()
    assert stopped.in_view() and not stopped.in_view(show_stopped=False)
    assert stopped.allows_control("comms_start", owner_pid=123)
    assert not DeletingThreadStatus().in_view(show_stopped=True, show_archived=True)
    for live in (RunningThreadStatus(), IdleThreadStatus()):
        assert live.restored() == stopped
        assert not live.allows_control("comms_start", owner_pid=123)
        assert live.allows_control("comms_start", owner_pid=0)
