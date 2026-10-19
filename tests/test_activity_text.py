"""New status text is bounded by activity; saved text is never repaired on read."""

import os
from dataclasses import replace

import pytest

from agent_comms.activity import Activity, ActivityState, ObservedActivity, ReadyDrainReadiness
from agent_comms.child_process import ProcessIdentity
from agent_comms.runtime_updates import ThreadOutput
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.display_order import LastActivityThreadSort
from agent_comms.ui_model.observation import ObserveSidebar, SidebarObserved
from agent_comms.ui_model.sidebar import SidebarModel
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_events import ToolExecutionStart
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.threads import Thread
from agent_comms.turn_phase import ModelWaitPhase


@pytest.mark.parametrize("length", [0, Activity.detail_limit - 1, Activity.detail_limit,
                                    Activity.detail_limit + 1, Activity.detail_limit * 50])
def test_new_activity_bounds_text_without_changing_short_text(length):
    detail = "é" * length
    activity = Activity.create("worker", ActivityState.WORKING, detail)
    assert len(activity.detail) <= Activity.detail_limit
    if length <= Activity.detail_limit:
        assert activity.detail == detail
    else:
        assert activity.detail.endswith("…")
        assert activity.detail.removesuffix("…") in detail
    assert FieldCodec.decode(Activity, FieldCodec.encode(activity)) == activity


@pytest.mark.parametrize("activity", [
    Activity("worker", ActivityState.THINKING, timestamp=123.0),
    ObservedActivity("worker", ActivityState.THINKING, timestamp=123.0,
                     readiness=ReadyDrainReadiness()),
])
def test_projection_preserves_source_identity_and_strict_record_validation(activity):
    detail = "tool_" + "x" * Activity.detail_limit
    projected = activity.with_status(ActivityState.WORKING, detail)
    assert type(projected) is type(activity)
    assert projected.thread == activity.thread
    assert projected.timestamp == activity.timestamp
    assert projected.detail.endswith("…")
    assert activity.detail == ""
    if isinstance(activity, ObservedActivity):
        assert projected.readiness is activity.readiness
    raw = FieldCodec.encode(activity)
    raw["detail"] = detail
    with pytest.raises(ValueError, match="Activity detail cannot exceed"):
        FieldCodec.decode(type(activity), raw)
    with pytest.raises(ValueError, match="Activity detail cannot exceed"):
        Activity.from_wire(FieldCodec.encode(Activity("worker", ActivityState.WORKING)) | {"detail": detail})


def test_native_status_reads_long_preparation_first_and_parallel_tools(comms):
    thread = comms.registry.declare(comms.registry.new_thread('worker', frozenset(), str(comms.root), process_identity=ProcessIdentity.capture(os.getpid())))
    detail = "preparing_" + "p" * Activity.detail_limit
    lease = comms.agents.begin_turn(thread.name, "long-status", detail).turn_lease
    try:
        assert comms.registry.require(thread.name).turn_state.active.phase.detail == detail
        assert comms.agents.activity_of(thread.name, snapshot=comms.registry.snapshot()).detail.endswith("…")
        phase = ModelWaitPhase()
        for index in range(2):
            title = f"tool_{index}_" + "x" * Activity.detail_limit
            phase = phase.on(ToolExecutionStart(tool_call_id=str(index), tool_name=title),
                             {str(previous) for previous in range(index)})
            comms.agents.observe_native_phase(lease, phase)
            activity = comms.agents.activity_of(thread.name, snapshot=comms.registry.snapshot())
            assert activity.state is ActivityState.WORKING
            assert activity.detail.startswith(f"tool_{index}_")
            assert len(activity.detail) == Activity.detail_limit
            assert phase.summary == title
    finally:
        comms.agents.finish_turn(lease)


def test_native_and_cli_activity_publish_member_order_and_ignore_unchanged_phases(comms):
    native = comms.registry.declare(comms.registry.new_thread('native-clock', frozenset({'clock'}), str(comms.root), process_identity=ProcessIdentity.capture(os.getpid())))
    cli = comms.registry.declare(comms.registry.new_thread('cli-clock', frozenset({'clock'}), str(comms.root), execution=ExternalThreadExecution))
    comms.channels.set_channel_sort("#clock", LastActivityThreadSort)
    output = ThreadOutput(ExternalTranscript(comms.root, cli.incarnation), comms.agents)
    request = ObserveSidebar(str(comms.root), True, False)

    def observed():
        snapshot = comms.views.viewer_snapshot(str(comms.root))
        return SidebarObserved(comms.views.revision(), request, snapshot, SidebarModel.derive(snapshot))

    def members(result):
        return result.derived[0]["#clock"].members

    output.status(ActivityState.WORKING, "unchanged")
    lease = comms.agents.begin_turn(native.name, "clock-order").turn_lease
    try:
        started = observed()
        assert members(started) == (native.name, cli.name)
        assert comms.agents.activity_of(native.name, snapshot=comms.registry.snapshot()).timestamp >= comms.registry.require(native.name).turn_state.active.started_at
        output.status(ActivityState.WORKING, "unchanged")
        external = observed()
        assert members(external) == (cli.name, native.name)
        assert not external.same_observation(started)
        phase = ModelWaitPhase().tool_started("clock-tool", "Clock progress")
        comms.agents.observe_native_phase(lease, phase)
        progressed = observed()
        assert members(progressed) == (native.name, cli.name)
        assert not progressed.same_observation(external)
        clock = comms.agents.activity_of(native.name, snapshot=comms.registry.snapshot()).timestamp
        assert comms.agents.observe_native_phase(lease, phase) == ()
        assert comms.agents.activity_of(native.name, snapshot=comms.registry.snapshot()).timestamp == clock
        assert observed().same_observation(progressed)
    finally:
        comms.agents.finish_turn(lease)
    finished = comms.agents.activity_of(native.name, snapshot=comms.registry.snapshot()).timestamp
    assert comms.agents.observe_native_phase(lease, ModelWaitPhase()) == ()
    assert comms.agents.activity_of(native.name, snapshot=comms.registry.snapshot()).timestamp == finished


def test_cli_keeps_whitespace_formatting_but_uses_activity_bound(comms):
    thread = comms.registry.declare(comms.registry.new_thread('cli-worker', frozenset(), str(comms.root), execution=ExternalThreadExecution, process_identity=ProcessIdentity.capture(os.getpid())))
    output = ThreadOutput(ExternalTranscript(comms.root, thread.incarnation), comms.agents)
    output.status(ActivityState.WORKING, "  tool:\n\t" + "x" * Activity.detail_limit)
    activity = comms.agents.activity_of(thread.name, snapshot=comms.registry.snapshot())
    assert activity.detail.startswith("tool: x")
    assert activity.detail.endswith("…")
    assert len(activity.detail) == Activity.detail_limit
    assert "\n" not in activity.detail and "\t" not in activity.detail
    output.status(ActivityState.WORKING, "  short:\n\ttext  ")
    assert comms.agents.activity_of(thread.name, snapshot=comms.registry.snapshot()).detail == "short: text"
    shown = comms.views.thread_presentations((thread.name,)).require(thread.name)
    assert shown.status.working
    assert shown.summary == "Working · short: text"
    assert shown.marker == "⌛"
    attention = replace(shown, status=shown.status.with_attention())
    assert ExternalThreadExecution.presentation(thread, comms.registry.snapshot().status(thread.name), attention, attention.binding) == attention
    output.status(ActivityState.IDLE)
    assert not comms.views.thread_presentations((thread.name,)).require(thread.name).status.working
