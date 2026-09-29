"""Current S1 consumer effects and fenced settlement, with real durable owners."""

import asyncio
import os
from abc import abstractmethod
from pathlib import Path

import pytest

from agent_comms import agent_events as ae
from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.goal_actions import GoalPrecondition, SetGoalAction, StandbyGoalAction
from agent_comms.owned_turn import OwnedTurn
from agent_comms.threads import Thread
from test_backend_native_lifecycle import native_backend as native_backend


class EffectCase(DeclaredFamily, affix="Case"):
    """Case-owned streams/expectations, discovered without a hand-maintained roster."""

    @abstractmethod
    def body(self): ...

    successful = True

    async def assert_effect(self, execution, progress):
        assert progress.terminal_ok is self.successful


class NormalCase(EffectCase):
    def body(self):
        return (ae.Chunk("answer"),)


class ToolCase(EffectCase):
    def body(self):
        return (
            ae.ToolStart("tool", "read", "Read source", {"path": "file"}),
            ae.ToolProgress("tool", "read", "reading"),
            ae.ToolEnd("tool", "read", True, "read"),
            ae.Chunk("answer"),
        )

    async def assert_effect(self, execution, progress):
        await super().assert_effect(execution, progress)
        assert progress.goals.successful_tool_observed


class CompactionCase(EffectCase):
    def body(self):
        return (
            ae.CompactionStart("test"),
            ae.CompactionProgress(1),
            ae.ToolStart("tool", "read", "Read after summary"),
            ae.CompactionEnd(summary="summary"),
            ae.AgentInfo(model="model", session_name="saved", context_used=20, context_size=1000),
            ae.Chunk("answer"),
        )

    async def assert_effect(self, execution, progress):
        await super().assert_effect(execution, progress)
        assert progress.compaction_resume_activity is None
        info = execution.runner.comms.agents.agent_info_of(execution.thread_name)
        assert info.context_used == 20 and info.context_size == 1000


class CompactionAbortCase(CompactionCase):
    def body(self):
        return (
            ae.CompactionStart("test"),
            ae.CompactionEnd(aborted=True),
            ae.AgentInfo(model="model", session_name="saved", context_used=20, context_size=1000),
            ae.Chunk("answer"),
        )


class SettingsCase(EffectCase):
    def body(self):
        return (
            ae.ModelChanged("same", True),
            ae.ThinkingChanged("same", False, "level unavailable"),
            ae.ModelChanged("same", False, "late"),
            ae.Chunk("answer"),
        )


class DiagnosticFailureCase(EffectCase):
    successful = False

    def body(self):
        return (
            ae.Chunk("provisional"),
            ae.Error("provider failed"),
        )


class ReasonFailureCase(DiagnosticFailureCase):
    def body(self):
        return (
            ae.Chunk("provisional"),
            ae.Error("provider failed", "backend_failed"),
        )


class InterruptedCase(EffectCase):
    def body(self):
        return (ae.Chunk("stale"), ae.SteeringInterrupted(), ae.Chunk("answer"))

    async def assert_effect(self, execution, progress):
        await super().assert_effect(execution, progress)
        assert progress.reply_parts == ["answer"]


@pytest.fixture
async def owner_turn(comms, tmp_path):
    owner = CommsAgent(
        comms,
        agent_bin="unused",
        private_nk_wire_root_id=comms.messaging.initialize_private_initial_protocol(),
        private_nk_native_package=tmp_path,
    )
    session = await owner.new_session(cwd=str(tmp_path), mcp_servers=[])
    name = owner.sessions.bindings[session.session_id]
    execution = OwnedTurn(owner.turns, session.session_id, name, "work", reply_targets=("#comms",))
    assert execution.admit()
    execution.begin()
    execution.prepare_prompt()
    execution.open_stream()
    progress = execution.progress
    try:
        yield execution, progress
    finally:
        if comms.registry.require(name).active_turn is not None:
            await owner.turns.settle_turn(
                session.session_id, name, execution.turn_id, execution.turn_lease
            )
        await owner.shutdown()


def waiting_owner(execution):
    comms = execution.runner.comms
    comms.threads.register(
        Thread(
            "waiting",
            frozenset(),
            execution.thread.worktree,
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    goal = comms.goals.update_goal("waiting", SetGoalAction(text="Review dependency"))
    comms.goals.update_goal(
        "waiting",
        StandbyGoalAction(
            expect=GoalPrecondition(goal_id=goal.id), wait_for=(execution.thread_name,)
        ),
    )
    return goal


@pytest.mark.parametrize("case", EffectCase.members_with(EffectCase), ids=lambda c: c.declared_name)
async def test_current_stream_effects_then_terminal_release(owner_turn, monkeypatch, case):
    execution, progress = owner_turn
    runner, comms = execution.runner, execution.runner.comms
    waiting_owner(execution)
    observed = []
    emit = runner.effects._emit_event

    async def observe(session, event, **kwargs):
        observed.append(type(event))
        await emit(session, event, **kwargs)

    monkeypatch.setattr(runner.effects, "_emit_event", observe)
    model = runner.sessions.config.setting_requests.add(ae.ModelChanged, "same")
    thinking = runner.sessions.config.setting_requests.add(ae.ThinkingChanged, "same")
    value = case()
    await progress.consume(ae.InputStarted(None))
    for event in value.body():
        await progress.consume(event)
        if isinstance(event, ae.ToolStart) and progress.compaction_resume_activity is None:
            assert comms.agents.activity_of(execution.thread_name).detail == event.title
        if isinstance(event, ae.CompactionStart):
            assert comms.agents.activity_of(execution.thread_name).detail == "Compacting context"
    await progress.consume(ae.StreamSettled())
    assert execution.finish_event.is_set()
    assert comms.registry.require(execution.thread_name).active_turn is None
    assert comms.goals.goal_wait("waiting") is not None
    diagnostic = {"elapsed_ms": 3} if not value.successful else None
    await progress.consume(
        ae.Done(
            "answer" if value.successful else "provider failed",
            value.successful,
            "backend_failed" if not value.successful else None,
            diagnostic,
        )
    )
    await value.assert_effect(execution, progress)
    # Public goal synchronization follows observed events and leaves the actual
    # execution signature current; no assertion about collaborator call counts.
    assert execution.session_id in runner.goal_execution_signatures
    assert (
        comms.goals.goal_changed(
            execution.thread_name, runner.goal_execution_signatures[execution.session_id]
        )
        is None
    )
    assert comms.goals.goal_wait("waiting") is not None
    await progress.publish_result()
    messages = comms.bus.log.full_history()
    if value.successful:
        assert any(row.body == "answer" and not row.notice for row in messages)
    else:
        assert not any(row.body == "provisional" for row in messages)
        assert any(row.notice and "[Open diagnostic]" in row.body for row in messages)
        assert progress.terminal_failure["diagnostic"] == diagnostic
    await runner.settle_turn(
        execution.session_id,
        execution.thread_name,
        execution.turn_id,
        execution.turn_lease,
        stream_settled=progress.settled,
        terminal_fence=progress.terminal_fence,
    )
    assert comms.goals.goal_wait("waiting") is None
    assert comms.registry.require("waiting").goal.state.active
    assert observed.index(ae.StreamSettled) < observed.index(ae.Done)
    if issubclass(case, SettingsCase):
        assert await model is None
        with pytest.raises(RuntimeError, match="level unavailable"):
            await thinking
    else:
        model.cancel()
        thinking.cancel()


@pytest.mark.parametrize("after_stream", [False, True])
async def test_transport_error_still_releases_real_wait_once(owner_turn, monkeypatch, after_stream):
    execution, progress = owner_turn
    comms, runner = execution.runner.comms, execution.runner
    waiting_owner(execution)
    if after_stream:
        await progress.consume(ae.StreamSettled())
    original = runner.effects._emit_event

    async def disconnected(session, event, **kwargs):
        await original(session, event, **kwargs)
        if isinstance(event, ae.TurnSettled):
            raise ConnectionError("client disconnected after receiving terminal event")

    monkeypatch.setattr(runner.effects, "_emit_event", disconnected)
    kwargs = dict(stream_settled=progress.settled, terminal_fence=progress.terminal_fence)
    if after_stream:
        await runner.settle_turn(
            execution.session_id,
            execution.thread_name,
            execution.turn_id,
            execution.turn_lease,
            **kwargs,
        )
    else:
        with pytest.raises(ConnectionError, match="client disconnected"):
            await runner.settle_turn(
                execution.session_id,
                execution.thread_name,
                execution.turn_id,
                execution.turn_lease,
                **kwargs,
            )
    assert comms.goals.goal_wait("waiting") is None
    goal = comms.registry.require("waiting").goal
    assert comms.goals.release_waits_after_terminal_turn(progress.terminal_fence) == ()
    assert comms.registry.require("waiting").goal == goal


@pytest.mark.parametrize("provider_status", [200, 503])
async def test_actual_native_stream_reaches_current_consumer_and_settlement(
    native_backend, monkeypatch, provider_status
):
    native = native_backend
    native.provider.status = provider_status
    comms = Comms(native.root)
    owner = CommsAgent(
        comms,
        agent_bin="pi",
        private_nk_wire_root_id=os.environ["AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"],
        private_nk_native_package=Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]),
    )
    session = await owner.new_session(cwd=str(native.project), mcp_servers=[])
    name = owner.sessions.bindings[session.session_id]
    execution = OwnedTurn(
        owner.turns, session.session_id, name, "native work", reply_targets=("#comms",)
    )
    assert execution.admit()
    execution.begin()
    execution.prepare_prompt()
    execution.open_stream()
    progress = execution.progress
    waiting_owner(execution)
    try:
        records = await native.run("native acceptance")
        for event in records:
            await progress.consume(event)
        assert progress.terminal_ok is (provider_status == 200)
        assert progress.settled
        assert comms.goals.goal_wait("waiting") is not None
        await progress.publish_result()
        await owner.turns.settle_turn(
            session.session_id,
            name,
            execution.turn_id,
            execution.turn_lease,
            stream_settled=progress.settled,
            terminal_fence=progress.terminal_fence,
        )
        assert comms.goals.goal_wait("waiting") is None
        assert native.provider.posts == 1
        assert len(native.starts) == 1
        rows = comms.bus.log.full_history()
        if provider_status == 200:
            assert any(row.body == "Native response lifecycle." for row in rows)
        else:
            assert any(row.notice and "[Open diagnostic]" in row.body for row in rows)
            assert not any(row.body == "Native response lifecycle." for row in rows)
    finally:
        await owner.shutdown()


@pytest.mark.parametrize("enabled", [False, True])
async def test_manual_bridge_real_native_terminal_releases_dependency(
    native_backend, monkeypatch, enabled
):
    import json

    from agent_comms.manual_compaction_bridge import compact_context
    from agent_comms.transcript_updates import StartedTranscriptUpdate

    native = native_backend
    await native.run(
        "History to compact\n" + "retained history material\n" * (10000 if enabled else 1000)
    )
    if enabled:
        await native.run("Second large completed exchange\n" + "history material\n" * 10000)
    await native.run("Recent final exchange")
    await native.persistent.close()
    config = Path(os.environ["AGENT_COMMS_NATIVE_CONFIG_DIR"])
    settings = json.loads((config / "settings.json").read_text())
    settings["compaction"] = {"enabled": enabled, "reserveTokens": 128, "keepRecentTokens": 32}
    (config / "settings.json").write_text(json.dumps(settings))
    comms = Comms(native.root)
    owner = CommsAgent(
        comms,
        agent_bin="pi",
        private_nk_wire_root_id=os.environ["AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"],
        private_nk_native_package=Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]),
    )
    session = await owner.new_session(cwd=str(native.project), mcp_servers=[])
    name = owner.sessions.bindings[session.session_id]
    comms.threads.set_thread_model(name, "response-local/fixture")
    comms.threads.attach_session(name, str(native.session))
    observed = []
    emit = owner._emit_event

    async def observe(session_id, event, **kwargs):
        await emit(session_id, event, **kwargs)
        observed.append(type(event))
        if isinstance(event, StartedTranscriptUpdate):
            comms.threads.register(
                Thread(
                    "waiting",
                    frozenset(),
                    str(native.project),
                    process_identity=ProcessIdentity.capture(os.getpid()),
                )
            )
            goal = comms.goals.update_goal("waiting", SetGoalAction(text="Wait for maintenance"))
            comms.goals.update_goal(
                "waiting",
                StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=(name,)),
            )
        if isinstance(event, ae.ManualCompactionEnd):
            assert comms.goals.goal_wait("waiting") is not None

    monkeypatch.setattr(owner, "_emit_event", observe)
    try:
        async with asyncio.timeout(40):
            result = await compact_context(owner.turns, session.session_id)
        print(
            "manual_bridge_result",
            enabled,
            result["ok"],
            native.provider.posts,
            result.get("error", ""),
        )
        assert result["ok"] is enabled
        assert native.provider.posts == (4 if enabled else 2)
        if enabled:
            rows = [json.loads(line) for line in native.session.read_text().splitlines()]
            assert len([row for row in rows if row.get("type") == "compaction"]) == 1
        else:
            assert result["error"] == "Selected native history has no complete safe compaction cut"
        assert comms.registry.require(name).active_turn is None
        assert comms.goals.goal_wait("waiting") is None
        assert comms.registry.require("waiting").goal.state.active
        assert observed.index(ae.ManualCompactionEnd) < observed.index(ae.TurnSettled)
        assert len(native.saved_inputs()) == (3 if enabled else 2)  # No input replay.
    finally:
        await owner.shutdown()


async def test_recovery_failure_report_preserves_original_and_does_not_duplicate(
    owner_turn, monkeypatch
):
    execution, progress = owner_turn
    emitted = []
    emit = execution.runner.effects._emit_event

    async def disconnect_after_error(session, event, **kwargs):
        await emit(session, event, **kwargs)
        if isinstance(event, ae.Error):
            emitted.append(event)
            raise ConnectionError("error notification transport closed")

    monkeypatch.setattr(execution.runner.effects, "_emit_event", disconnect_after_error)
    original = ValueError("original failure")
    await progress.report_failure(original)
    await progress.report_failure(original)
    assert [event.text for event in emitted] == ["original failure"]
    assert progress.failure_reported


async def test_relay_entrypoint_terminal_publication_releases_real_wait(
    comms, tmp_path, monkeypatch
):
    from acp.schema import TextContentBlock

    from agent_comms.transcript_updates import StartedTranscriptUpdate

    owner = CommsAgent(
        comms,
        agent_bin="unused",
        no_reply_window=0,
        private_nk_wire_root_id=comms.messaging.initialize_private_initial_protocol(),
        private_nk_native_package=tmp_path,
    )
    session = await owner.new_session(cwd=str(tmp_path), mcp_servers=[])
    name = owner.sessions.bindings[session.session_id]
    events = []
    emit = owner._emit_event

    async def observed(session_id, event, **kwargs):
        await emit(session_id, event, **kwargs)
        events.append(type(event))
        if isinstance(event, StartedTranscriptUpdate):
            comms.threads.register(
                Thread(
                    "waiting",
                    frozenset(),
                    str(tmp_path),
                    process_identity=ProcessIdentity.capture(os.getpid()),
                )
            )
            goal = comms.goals.update_goal("waiting", SetGoalAction(text="Review relay outcome"))
            comms.goals.update_goal(
                "waiting",
                StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=(name,)),
            )
        if isinstance(event, ae.TurnSettled):
            assert comms.goals.goal_wait("waiting") is not None
            assert comms.registry.require(name).active_turn is None

    monkeypatch.setattr(owner, "_emit_event", observed)
    try:
        async with asyncio.timeout(10):
            await owner.turns.prompt_owned(
                session.session_id, [TextContentBlock(type="text", text="!relay #comms relay body")]
            )
        assert comms.goals.goal_wait("waiting") is None
        assert comms.registry.require("waiting").goal.state.active
        assert events.count(ae.TurnSettled) == 1
        assert any(row.body == "relay body" for row in comms.bus.log.full_history())
    finally:
        await owner.shutdown()
