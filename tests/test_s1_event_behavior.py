"""Current S1 consumer effects and fenced settlement, with real durable owners."""

import asyncio
import os
from abc import abstractmethod
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import uuid4

import pytest
from acp import RequestError

import agent_comms.agent_events as ae
from agent_comms.acp import CommsAgent
from agent_comms.agent_backend import PlacedCompaction
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.goal_actions import GoalPrecondition, SetGoalAction, StandbyGoalAction
from agent_comms.goal_waits import NoGoalWait, RecordedGoalWait
from agent_comms.messages import InfoKind, Message, NoticeRole
from agent_comms.owned_send import OwnedSend
from agent_comms.owned_turn import OwnedTurn
from agent_comms.pi_counts import PiCount
from agent_comms.thread_identity import TurnId
from agent_comms.threads import Thread
from agent_comms.transcript_updates import TurnTranscriptUpdate
from bus_history import read_history
from stored_inputs import stored_inputs

pytestmark = pytest.mark.usefixtures("fake_native_catalog")


class EffectCase(DeclaredFamily, affix="Case"):
    """Case-owned streams/expectations, discovered without a hand-maintained roster."""

    @abstractmethod
    def body(self): ...

    successful = True

    async def assert_effect(self, execution, progress):
        assert progress.result is not None and progress.result.ok is self.successful


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
            ae.AgentInfo(model="model", context_size=PiCount.from_wire(1000)),
            ae.ContextUsageObservation(context_used=PiCount.from_wire(20)),
            ae.Chunk("answer"),
        )

    async def assert_effect(self, execution, progress):
        await super().assert_effect(execution, progress)
        info = execution.runner.comms.agents.agent_info_of(execution.thread_name)
        assert info.context_used == PiCount.from_wire(20) and info.context_size == PiCount.from_wire(1000)


class CompactionAbortCase(CompactionCase):
    def body(self):
        return (
            ae.CompactionStart(),
            ae.CompactionEnd(aborted=True),
            ae.AgentInfo(model="model", context_size=PiCount.from_wire(1000)),
            ae.ContextUsageObservation(context_used=PiCount.from_wire(20)),
            ae.Chunk("answer"),
        )


class ContextCase(EffectCase):
    def body(self):
        return (
            ae.AgentInfo(model="model", context_size=PiCount.from_wire(1000)),
            ae.ContextUsageObservation(context_used=PiCount.from_wire(20)),
            ae.ContextInfo(context_used=PiCount.from_wire(None), context_size=PiCount.from_wire(1000)),
            ae.ContextInfo(context_used=PiCount.from_wire(35), context_size=PiCount.from_wire(1000)),
            ae.Chunk("answer"),
        )

    async def assert_effect(self, execution, progress):
        await super().assert_effect(execution, progress)
        info = execution.runner.comms.agents.agent_info_of(execution.thread_name)
        assert info.model == "model"
        assert (info.context_used, info.context_size) == (35, 1000)


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
async def owner_turn(comms, tmp_path, fake_native_catalog):
    owner = CommsAgent(
        comms,
        agent_bin="unused",
    )
    session = await owner.new_session(cwd=str(tmp_path), mcp_servers=[])
    name = owner.sessions.bindings[session.session_id]
    origin = comms.messaging.send_user_message(name, "work", worktree=str(tmp_path))
    execution = OwnedTurn(
        owner.turns, session.session_id, name, "work", origins=(origin,)
    )
    try:
        async with AsyncExitStack() as resources:
            async with AsyncExitStack() as permits:
                assert await execution.acquire(resources, permits)
                yield execution, execution.progress
    finally:
        await owner.shutdown()


async def start_owned_input(execution, progress):
    claim_scope = OwnedSend(
        comms=execution.runner.comms,
        owner_name=execution.thread.name,
        inputs=execution.runner.inputs,
        goal_store=execution.runner.comms.goals.open_goal_store(),
        session_id=execution.session_id,
        thread=execution.thread,
        turn=TurnId(execution.turn_id),
        claim_scope=execution.turn_lease.claimed_generation,
        original=execution.original,
        goal_permit=execution.goal_permit,
    )
    native_id = uuid4().hex
    with claim_scope(None, native_id, execution.original.prompt):
        pass
    assert claim_scope.native_start(None, native_id, execution.original.prompt)
    await progress.consume(ae.OriginalInputStarted())


def waiting_owner(execution):
    comms = execution.runner.comms
    comms.registry.declare(
        comms.registry.new_thread('waiting', frozenset(), execution.thread.worktree, process_identity=ProcessIdentity.capture(os.getpid()))
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
    await start_owned_input(execution, progress)
    for event in value.body():
        await progress.consume(event)
        if isinstance(event, ae.CompactionStart):
            assert comms.agents.activity_of(execution.thread_name, snapshot=comms.registry.snapshot()).detail == "Compacting context"
    await progress.consume(ae.StreamSettled())
    assert execution.finish_event.is_set()
    assert comms.registry.require(execution.thread_name).turn_lease == execution.turn_lease
    assert isinstance(comms.goals.goal_wait("waiting"), RecordedGoalWait)
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
    # goal snapshot current; no assertion about collaborator call counts.
    assert execution.session_id in runner.goals.goal_snapshots
    assert (
        comms.goals.goal_changed(
            execution.thread_name, runner.goals.goal_snapshots[execution.session_id]
        )
        is None
    )
    assert isinstance(comms.goals.goal_wait("waiting"), RecordedGoalWait)
    if not value.successful:
        with pytest.raises(RequestError, match="Internal error"):
            await progress.publish_result()
        messages = read_history(comms)
        assert not any(row.body == "provisional" for row in messages)
        assert any(isinstance(row.role, NoticeRole) and "[Open diagnostic]" in row.body for row in messages)
        assert progress.result is not None and progress.result.diagnostic == diagnostic
    await runner.settle_turn(
        execution.session_id,
        execution.turn_lease,
    )
    assert isinstance(comms.goals.goal_wait("waiting"), NoGoalWait)
    assert comms.registry.require("waiting").goal.state.active
    assert observed.index(ae.StreamSettled) < observed.index(ae.Done)
    if issubclass(case, SettingsCase):
        assert ae.ModelChanged in observed and ae.ThinkingChanged in observed


async def test_usage_observation_keeps_metadata_without_reattachment(owner_turn, monkeypatch):
    execution, progress = owner_turn
    comms, name = execution.runner.comms, execution.thread_name
    comms.agents.observe_runtime_info(name, ae.ContextUsageObservation(context_used=PiCount.from_wire(20)))
    comms.agents.observe_runtime_info(name, ae.AgentInfo(
        model="test/model", context_size=PiCount.from_wire(1000),
    ))

    async def unexpected_metadata(event):
        pytest.fail("Usage observation invoked native identity/configuration lifecycle")

    monkeypatch.setattr(progress, "before_native_metadata", unexpected_metadata)
    monkeypatch.setattr(progress, "after_native_metadata", unexpected_metadata)

    class SampledContext(ae.ContextInfo):
        """Another producer needs no new dispatch or persistence enrollment."""

    await progress.consume(SampledContext(context_used=PiCount.from_wire(None), context_size=PiCount.from_wire(1000)))
    info = comms.agents.agent_info_of(name)
    assert info.model == "test/model"
    assert (info.context_used, info.context_size) == (None, 1000)
    await progress.consume(SampledContext(context_used=PiCount.from_wire(35), context_size=PiCount.from_wire(1000)))
    comms.agents.observe_runtime_info(name, ae.AgentInfo(
        model="test/new-model", context_size=PiCount.from_wire(2000),
    ))
    info = comms.agents.agent_info_of(name)
    assert info.model == "test/new-model"
    assert (info.context_used, info.context_size) == (35, 2000)


@pytest.mark.parametrize("after_stream", [False, True])
async def test_transport_error_still_releases_real_wait_once(owner_turn, monkeypatch, after_stream):
    execution, progress = owner_turn
    comms, runner = execution.runner.comms, execution.runner
    waiting_owner(execution)
    await start_owned_input(execution, progress)
    if after_stream:
        await progress.consume(ae.StreamSettled())
    original = runner.effects._emit_event

    async def disconnected(session, event, **kwargs):
        await original(session, event, **kwargs)
        if isinstance(event, TurnTranscriptUpdate) and not event.state.active.phase.busy:
            raise ConnectionError("client disconnected after receiving terminal event")

    released = []
    release_waits = comms.goals.release_waits_after_terminal_turn

    def capture_release(fence):
        released.append(fence)
        return release_waits(fence)

    monkeypatch.setattr(comms.goals, "release_waits_after_terminal_turn", capture_release)
    monkeypatch.setattr(runner.effects, "_emit_event", disconnected)
    with pytest.raises(ConnectionError, match="client disconnected"):
        await runner.settle_turn(
            execution.session_id,
            execution.turn_lease,
        )
    monkeypatch.setattr(runner.effects, "_emit_event", original)
    assert isinstance(comms.goals.goal_wait("waiting"), NoGoalWait)
    goal = comms.registry.require("waiting").goal
    assert len(released) == 1 and release_waits(released[0]) == ()
    assert comms.registry.require("waiting").goal == goal


@pytest.mark.parametrize(
    ("provider_status", "committed_progress"),
    [(200, False), (200, True), (503, False)],
    ids=["final", "tool-progress-final", "failed-provider"],
)
async def test_actual_native_stream_reaches_current_consumer_and_settlement(
    native_backend, monkeypatch, provider_status, committed_progress
):
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
    )
    await owner._runtime.start()
    session = await owner.new_session(cwd=str(native.project), mcp_servers=[])
    name = owner.sessions.bindings[session.session_id]
    comms.threads.attach_session((attachment_owner := comms.registry.require(name)), str(native.session), process=attachment_owner.process_identity)
    origin = comms.messaging.send_user_message(
        name, "native acceptance", worktree=str(native.project)
    )
    execution = OwnedTurn(
        owner.turns, session.session_id, name, "native acceptance", origins=(origin,)
    )
    try:
        if provider_status == 200:
            await execution.run()
        else:
            with pytest.raises(RequestError):
                await execution.run()
        assert not comms.registry.require(name).active_turn.executing
        assert not owner.inputs.state(session.session_id).turn_open()
        if provider_status == 200:
            assert native.provider.posts == (2 if committed_progress else 1)
        inputs = stored_inputs(comms.root).rows
        assert len(inputs) == 1 and all(row.has_started for row in inputs.values())
        rows = read_history(comms)
        replies = [row for row in rows if row.sender == name]
        if provider_status == 200:
            assert [(row.body, isinstance(row.role, NoticeRole)) for row in replies] == (
                [("Working", True), ("Native response lifecycle.", True)]
                if committed_progress else [("Native response lifecycle.", True)]
            )
            page = comms.transcripts.capture_page_read(name).read()
            final = [
                event for event in page.events
                if isinstance(event, TextTranscript) and event.text == "Native response lifecycle."
            ]
            assert len(final) == 1 and isinstance(final[0], SentTranscript)
            assert final[0].source == replies[-1].reference
            assert final[0].routing.reply.targets == (origin.sender,)
        else:
            assert len(replies) == 1 and isinstance(replies[0].role, NoticeRole)
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
    )
    await owner._runtime.start()
    session = await owner.new_session(cwd=str(native.project), mcp_servers=[])
    name = owner.sessions.bindings[session.session_id]
    comms.threads.set_thread_configuration(comms.registry.require(name), model='response-local/fixture')
    comms.threads.attach_session((attachment_owner := comms.registry.require(name)), str(native.session), process=attachment_owner.process_identity)
    observed = []
    emit = owner._emit_event

    async def observe(session_id, event, **kwargs):
        await emit(session_id, event, **kwargs)
        observed.append(event)
        if isinstance(event, TurnTranscriptUpdate) and event.state.active.phase.busy and "waiting" not in comms.registry:
            comms.registry.declare(
                comms.registry.new_thread('waiting', frozenset(), str(native.project), process_identity=ProcessIdentity.capture(os.getpid()))
            )
            goal = comms.goals.update_goal("waiting", SetGoalAction(text="Wait for maintenance"))
            comms.goals.update_goal(
                "waiting",
                StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=(name,)),
            )
        if isinstance(event, ae.CompactionEnd) and event.reason is ManualCompactionReason:
            assert isinstance(comms.goals.goal_wait("waiting"), RecordedGoalWait)

    monkeypatch.setattr(owner, "_emit_event", observe)
    try:
        async with asyncio.timeout(40):
            result = await compact_context(owner.turns, session.session_id)
        assert isinstance(result, PlacedCompaction), result
        assert native.provider.posts > 3  # Pi asked the provider for the summary.
        rows = [json.loads(line) for line in native.session.read_text().splitlines()]
        assert len([row for row in rows if row.get("type") == "compaction"]) == 1
        assert not comms.registry.require(name).active_turn.executing
        assert isinstance(comms.goals.goal_wait("waiting"), NoGoalWait)
        assert comms.registry.require("waiting").goal.state.active
        terminal = next(i for i, event in enumerate(observed)
            if isinstance(event, TurnTranscriptUpdate) and not event.state.active.phase.busy)
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
        if isinstance(event, ae.Done):
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
