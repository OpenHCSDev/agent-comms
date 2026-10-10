"""Current S1 consumer effects and fenced settlement, with real durable owners."""

import asyncio
import os
from contextlib import AsyncExitStack
from abc import abstractmethod
from pathlib import Path

import pytest

from agent_comms import agent_events as ae
from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.agent_backend import PlacedCompaction
from agent_comms.declared_family import DeclaredFamily
from agent_comms.goal_actions import GoalPrecondition, SetGoalAction, StandbyGoalAction
from agent_comms.owned_turn import OwnedTurn
from agent_comms.threads import Thread
from agent_comms.transcript_updates import TurnTranscriptUpdate
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
        assert progress.goals.productive_tool == self.body()[2]


class CompactionCase(EffectCase):
    def body(self):
        return (
            ae.CompactionStart(),
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
            ae.CompactionStart(),
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
    try:
        async with AsyncExitStack() as resources:
            async with AsyncExitStack() as permits:
                assert await execution.acquire(resources, permits)
                yield execution, execution.progress
    finally:
        await owner.shutdown()


def waiting_owner(execution):
    comms = execution.runner.comms
    comms.registry.declare(
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
    assert comms.registry.require(execution.thread_name).turn_lease == execution.turn_lease
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
    assert execution.session_id in runner.goals.goal_execution_signatures
    assert (
        comms.goals.goal_changed(
            execution.thread_name, runner.goals.goal_execution_signatures[execution.session_id]
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
        execution.turn_lease,
    )
    assert comms.goals.goal_wait("waiting") is None
    assert comms.registry.require("waiting").goal.state.active
    assert observed.index(ae.StreamSettled) < observed.index(ae.Done)
    if issubclass(case, SettingsCase):
        assert ae.ModelChanged in observed and ae.ThinkingChanged in observed


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
        if isinstance(event, TurnTranscriptUpdate) and not event.state.busy:
            raise ConnectionError("client disconnected after receiving terminal event")

    monkeypatch.setattr(runner.effects, "_emit_event", disconnected)
    with pytest.raises(ConnectionError, match="client disconnected"):
        await runner.settle_turn(
            execution.session_id,
            execution.turn_lease,
        )
    assert comms.goals.goal_wait("waiting") is None
    goal = comms.registry.require("waiting").goal
    assert comms.goals.release_waits_after_terminal_turn(progress.terminal_fence) == ()
    assert comms.registry.require("waiting").goal == goal


@pytest.mark.parametrize(
    ("provider_status", "committed_progress"),
    [(200, False), (200, True), (503, False)],
    ids=["final", "tool-progress-final", "failed-provider"],
)
async def test_actual_native_stream_reaches_current_consumer_and_settlement(
    native_backend, monkeypatch, provider_status, committed_progress
):
    from acp import RequestError

    from agent_comms.input_disposition import InputDispositions
    from agent_comms.transcript_events import SentTranscript, TextTranscript

    native = native_backend
    native.provider.status = provider_status
    if committed_progress:
        native.provider.tool_call = ("bash", {"command": "printf ordinary-publication-tool"})
        chunks = native.provider.response_chunks

        def with_progress():
            if native.provider.tool_call is not None:
                yield {"content": "Working"}, None
            yield from chunks()

        monkeypatch.setattr(native.provider, "response_chunks", with_progress)
    comms = Comms(native.root)
    owner = CommsAgent(
        comms,
        agent_bin="pi",
        agent_args=[
            "--provider", "response-local", "--model", "fixture", "--thinking", "off",
            "--offline", "--no-extensions", "--no-skills", "--no-context-files",
            "--no-prompt-templates",
        ],
        private_nk_wire_root_id=os.environ["AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"],
        private_nk_native_package=Path(os.environ["PI_COMPACTION_TEST_PACKAGE"]),
    )
    session = await owner.new_session(cwd=str(native.project), mcp_servers=[])
    name = owner.sessions.bindings[session.session_id]
    comms.threads.attach_session(name, str(native.session))
    execution = OwnedTurn(
        owner.turns, session.session_id, name, "native acceptance",
        reply_targets=("#comms",),
    )
    try:
        if provider_status == 200:
            await execution.run()
        else:
            with pytest.raises(RequestError):
                await execution.run()
        assert comms.registry.require(name).active_turn is None
        assert session.session_id not in owner.inputs.backend_inboxes
        assert native.provider.posts == (2 if committed_progress else 1)
        inputs = InputDispositions(comms.root / InputDispositions.filename).read().rows
        assert len(inputs) == 1 and all(row.has_started for row in inputs.values())
        rows = comms.bus.log.full_history()
        replies = [row for row in rows if row.sender == name]
        if provider_status == 200:
            assert [(row.body, row.notice) for row in replies] == (
                [("Working", True), ("Native response lifecycle.", False)]
                if committed_progress else [("Native response lifecycle.", False)]
            )
            page = comms.transcripts.capture_page_read(name).read()
            final = [
                event for event in page.events
                if isinstance(event, TextTranscript) and event.text == "Native response lifecycle."
            ]
            assert len(final) == 1 and isinstance(final[0], SentTranscript)
            assert final[0].source == replies[-1].reference
            assert final[0].routing.reply.targets == ("#comms",)
        else:
            assert len(replies) == 1 and replies[0].notice
            assert "[Open diagnostic]" in replies[0].body
            assert not any(row.body == "Native response lifecycle." for row in replies)
    finally:
        await owner.shutdown()


async def test_manual_bridge_real_native_terminal_releases_dependency(native_backend, monkeypatch):
    import json

    from agent_comms.manual_compaction_bridge import compact_context
    from agent_comms.pi_vocabulary import ManualCompactionReason

    native = native_backend
    await native.run("History to compact\n" + "retained history material\n" * 10000)
    await native.run("Second large completed exchange\n" + "history material\n" * 10000)
    await native.run("Recent final exchange")
    await native.persistent.close()
    config = Path(os.environ["AGENT_COMMS_NATIVE_CONFIG_DIR"])
    settings = json.loads((config / "settings.json").read_text())
    settings["compaction"] = {"enabled": True, "reserveTokens": 128, "keepRecentTokens": 32}
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
    comms.threads.set_thread_configuration(comms.registry.require(name), model='response-local/fixture')
    comms.threads.attach_session(comms.registry.require(name), str(native.session))
    observed = []
    emit = owner._emit_event

    async def observe(session_id, event, **kwargs):
        await emit(session_id, event, **kwargs)
        observed.append(event)
        if isinstance(event, TurnTranscriptUpdate) and event.state.busy and "waiting" not in comms.registry:
            comms.registry.declare(
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
        if isinstance(event, ae.CompactionEnd) and event.reason is ManualCompactionReason:
            assert comms.goals.goal_wait("waiting") is not None

    monkeypatch.setattr(owner, "_emit_event", observe)
    try:
        async with asyncio.timeout(40):
            result = await compact_context(owner.turns, session.session_id)
        assert isinstance(result, PlacedCompaction), result
        assert native.provider.posts > 3  # Pi asked the provider for the summary.
        rows = [json.loads(line) for line in native.session.read_text().splitlines()]
        assert len([row for row in rows if row.get("type") == "compaction"]) == 1
        assert comms.registry.require(name).active_turn is None
        assert comms.goals.goal_wait("waiting") is None
        assert comms.registry.require("waiting").goal.state.active
        terminal = next(i for i, event in enumerate(observed)
            if isinstance(event, TurnTranscriptUpdate) and not event.state.busy)
        compaction_end = next(i for i, event in enumerate(observed)
            if isinstance(event, ae.CompactionEnd) and event.reason is ManualCompactionReason)
        assert compaction_end < terminal
        assert len(native.saved_inputs()) == 3  # No input replay.
    finally:
        await owner.shutdown()


async def test_failure_publication_preserves_private_cause_after_transport_disconnect(
    owner_turn, monkeypatch
):
    import json

    execution, progress = owner_turn
    emitted = []
    emit = execution.runner.effects._emit_event

    async def disconnect_after_error(session, event, **kwargs):
        await emit(session, event, **kwargs)
        if isinstance(event, ae.Error):
            emitted.append(event)
            raise ConnectionError("error notification transport closed")

    monkeypatch.setattr(execution.runner.effects, "_emit_event", disconnect_after_error)
    original = ValueError("private original failure")
    await progress.report_failure(original)
    record = execution.runner.comms.root / "diagnostics" / (execution.turn_id + ".json")
    diagnostic = json.loads(record.read_text())
    assert "ValueError: private original failure" in diagnostic["source_error"]
    assert len(emitted) == 1
    assert "private original failure" not in emitted[0].text
    assert record.as_uri() in emitted[0].text
    assert diagnostic["outcome"] == "failed; inputs must not be replayed automatically"


async def test_relay_entrypoint_terminal_publication_releases_real_wait(
    comms, tmp_path, monkeypatch
):
    from acp.schema import TextContentBlock


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
        events.append(event)
        if isinstance(event, TurnTranscriptUpdate) and event.state.busy and "waiting" not in comms.registry:
            comms.registry.declare(
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
        if isinstance(event, TurnTranscriptUpdate) and not event.state.busy:
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
        assert sum(isinstance(event, TurnTranscriptUpdate) and not event.state.busy
            for event in events) == 1
        assert any(row.body == "relay body" for row in comms.bus.log.full_history())
    finally:
        await owner.shutdown()
