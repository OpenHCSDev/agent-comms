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


def test_captured_owner_presentation_survives_exit_until_fresh_acquisition(tmp_path):
    comms = wire(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        thread = Thread("owner", frozenset(), str(tmp_path),
                        process_identity=ProcessIdentity.capture(child.pid))
        comms.registry.register(thread, RunningThreadStatus())
        snapshot = comms.registry.snapshot()
        view = ThreadView.capture(thread, snapshot, comms.agents.activity_of("owner"), None, {})
        displayed = view.presentation
        # Cross the same original codec boundary used by worker-prepared views.
        transferred = FieldCodec.decode(ThreadView, FieldCodec.encode(view))
        child.terminate()
        child.wait(timeout=5)
        assert not thread.process_alive
        assert view.presentation == displayed
        assert transferred.presentation == displayed
        fresh = ThreadView.capture(thread, snapshot, comms.agents.activity_of("owner"), None, {})
        assert fresh.presentation.summary == "Owner exited"
        assert not fresh.presentation.status.busy
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize("status", [member() for member in ThreadStatus.members_with(ThreadStatus)])
def test_saved_presence_roundtrip_and_wire_views_preserve_data(tmp_path, status):
    comms = wire(tmp_path)
    thread = Thread(
        "owner",
        frozenset({"team"}),
        str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
        title="Retained title",
        session_file="/retained/session.jsonl",
        created_at=12.5,
    )
    comms.registry.register(thread, status)
    before = comms.registry.snapshot()
    raw = json.loads(comms.registry.store.path.read_text())
    assert FieldCodec.decode(ThreadStatus, raw["statuses"]["owner"]) == status
    assert raw["threads"]["owner"]["created_at"] == 12.5
    reopened = Registration(comms.registry.store.path)
    assert reopened.snapshot() == before
    assert reopened.status("owner") == status
    assert FieldCodec.decode(ThreadStatus, FieldCodec.encode(status)) == status
    assert comms.views.thread_detail("owner")["status"] == status.declared_name
    assert comms.views.list_threads()[0]["status"] == status.declared_name
    activity = Activity("owner", ActivityState.WORKING, "stale or current activity")
    comms.agents.activity.emit(activity)
    view = ThreadView.capture(thread, reopened.snapshot(), comms.agents.activity_of('owner'), None, {})
    assert view.to_wire()["status"] == status.declared_name
    assert view.presentation.status.busy == status.active
    visible = comms.views.thread_views()
    individual = comms.views.thread_presentation("owner")
    if visible:
        # Opening the individual acquires a transcript reader; the roster owns
        # none. Its original identity must bind this owner, while presentation
        # of the common lifecycle/readiness is identical.
        assert individual.read_identity.thread.incarnation == thread.incarnation
        assert replace(individual, read_identity=None) == visible[0].presentation
    else:
        assert individual is None
    assert comms.views.thread_presentation("missing") is None


@pytest.mark.parametrize("status", [RunningThreadStatus(), IdleThreadStatus()])
def test_live_owner_cannot_be_deleted_and_heartbeat_keeps_both_counters(tmp_path, status):
    registry = Registration(tmp_path / "registry.json")
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        ),
        status,
    )
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
    registry.register(
        Thread(
            "owner",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        ),
        status,
    )
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
    from agent_comms.tools import CommsStartTool, OwnerLifecycleControl, ToolRequest

    archived = ArchivedThreadStatus()
    assert archived.restored() == archived
    assert not archived.in_view()
    assert archived.in_view(show_archived=True)
    for command in ToolRequest.members_with(OwnerLifecycleControl):
        assert not command.available_for(archived, owner_pid=123)
    stopped = StoppedThreadStatus()
    assert stopped.in_view() and not stopped.in_view(show_stopped=False)
    assert CommsStartTool.available_for(stopped, owner_pid=123)
    assert not DeletingThreadStatus().in_view(show_stopped=True, show_archived=True)
    for live in (RunningThreadStatus(), IdleThreadStatus()):
        assert live.restored() == stopped
        assert not CommsStartTool.available_for(live, owner_pid=123)
        assert CommsStartTool.available_for(live, owner_pid=0)


def test_batch_presentations_acquire_documents_once_and_release_locks_before_rows(comms, monkeypatch):
    from collections import Counter
    from agent_comms.goal_waits import GoalWaits
    from agent_comms.locked_store import JsonDocument

    for name in ("alpha", "beta"):
        comms.registry.declare(Thread(name, frozenset(), str(comms.root)))
    comms.agents.runtime_info.replace({})
    waits = GoalWaits(comms.root / GoalWaits.filename)
    waits.replace({})
    counts = Counter()
    decode = JsonDocument._decode
    capture = ThreadView.capture.__func__

    def counted(self, data):
        if self.path.name in ("runtime_info.json", "goal_waits.json"):
            counts[self.path.name] += 1
        return decode(self, data)

    def unlocked(cls, *args, **kwargs):
        for store in (comms.agents.runtime_info, waits, comms.registry.store):
            with store.locked(blocking=False):
                pass
        return capture(cls, *args, **kwargs)

    monkeypatch.setattr(JsonDocument, "_decode", counted)
    monkeypatch.setattr(ThreadView, "capture", classmethod(unlocked))
    batch = comms.views.thread_presentations(("alpha", "beta", "missing"))
    assert set(batch.views) == {"alpha", "beta"}
    assert batch.read("missing") is None
    assert batch.read("alpha").read_identity.thread.incarnation == batch.views["alpha"].thread.incarnation
    assert batch.read("beta").read_identity.thread.incarnation == batch.views["beta"].thread.incarnation
    assert counts == {"runtime_info.json": 1, "goal_waits.json": 1}

    def unrelated(*args, **kwargs):
        pytest.fail("A missing thread acquired unrelated activity/documents")

    monkeypatch.setattr(comms.agents, "all_activity", unrelated)
    monkeypatch.setattr(JsonDocument, "_decode", unrelated)
    assert comms.views.thread_presentation("missing") is None


def test_batch_source_rename_keeps_the_original_registry_fence(comms):
    from agent_comms.coordination_errors import StaleRevision

    comms.registry.register(
        Thread("alpha", frozenset(), str(comms.root), process_identity=ProcessIdentity.capture(os.getpid())),
        RunningThreadStatus(),
    )
    batch = comms.views.thread_presentations(("alpha",))
    comms.registry.rename("alpha", "renamed")
    with pytest.raises(StaleRevision, match="incarnation changed"):
        batch.read("alpha")
    current = comms.views.thread_presentations(("alpha",))
    assert set(current.views) == {"renamed"}
    assert current.read("alpha").read_identity.thread.incarnation == comms.registry.require("renamed").incarnation


def test_observed_batch_keeps_source_busy_scoped_to_one_thread(comms, monkeypatch):
    from agent_comms.coordination_errors import CoordinationReadUnavailable
    from agent_comms.ui_model.observation import ObserveThreads, ThreadReading

    for name in ("alpha", "beta"):
        comms.registry.declare(Thread(name, frozenset(), str(comms.root)))
    capture = comms.transcripts.capture_page_window

    def read(name, **kwargs):
        if name == "alpha":
            raise CoordinationReadUnavailable("original source deferred")
        return capture(name, **kwargs)

    monkeypatch.setattr(comms.transcripts, "capture_page_window", read)
    reading = ThreadReading(ObserveThreads(frozenset({"alpha", "beta"})))
    list(reading.read(comms, comms.views.revision()))
    assert reading.observed.busy == {"alpha"}
    assert set(reading.observed.presentations) == {"beta"}
    assert reading.observed.unavailable == {}


def test_observed_batch_acquisition_busy_recovers_without_a_new_revision(comms, monkeypatch):
    from agent_comms.coordination_errors import CoordinationReadUnavailable
    from agent_comms.runtime_info import RuntimeInfoStore
    from agent_comms.ui_model.observation import ObserveThreads, ThreadReading

    for name in ("alpha", "beta"):
        comms.registry.declare(Thread(name, frozenset(), str(comms.root)))
    calls = []
    original = RuntimeInfoStore.read

    def busy(self):
        calls.append(self.path)
        raise CoordinationReadUnavailable("shared metadata deferred")

    reading = ThreadReading(ObserveThreads(frozenset({"alpha", "beta"})))
    revision = comms.views.revision()
    monkeypatch.setattr(RuntimeInfoStore, "read", busy)
    list(reading.read(comms, revision))
    assert calls == [comms.agents.runtime_info.path]
    assert reading.observed.busy == {"alpha", "beta"}
    assert reading.observed.presentations == {}
    monkeypatch.setattr(RuntimeInfoStore, "read", original)
    list(reading.read(comms, revision))
    assert not reading.observed.busy
    assert set(reading.observed.presentations) == {"alpha", "beta"}
    assert reading.observed.changed == {"alpha", "beta"}
