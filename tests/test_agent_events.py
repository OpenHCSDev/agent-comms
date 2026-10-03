"""Nominal extension, dispatch ordering, and shared settlement contracts."""

import ast
import asyncio
import os
import threading
from contextlib import AsyncExitStack
from functools import partial
from dataclasses import FrozenInstanceError, dataclass, replace
from pathlib import Path

import pytest

from agent_comms import agent_events as events
from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.activity import ActivityState
from agent_comms.mro_dispatch import MroDispatch, handles
from agent_comms.pending_requests import PendingRequests
from agent_comms.threads import Thread
from agent_comms.coordinator import Coordination
from agent_comms.transcript_updates import TurnTranscriptUpdate


async def test_mro_specific_before_shared_and_consumer_override():
    class Left(events.AgentEvent):
        pass

    class Right(events.AgentEvent):
        pass

    @dataclass(frozen=True)
    class Diamond(Left, Right):
        pass

    seen = []

    class Consumer(MroDispatch):
        @handles(Left)
        async def left(self, event):
            seen.append("left")

        @handles(Right)
        async def right(self, event):
            seen.append("right")

        @handles(events.AgentEvent)
        async def root(self, event):
            seen.append("root")

    class Override(Consumer):
        @handles(Left)
        async def left(self, event):
            seen.append("override")

    await Override().dispatch(Diamond())
    assert seen == ["override", "right", "root"]
    assert Diamond.__mro__.count(events.AgentEvent) == 1


@pytest.mark.parametrize("synchronous", [False, True])
async def test_selected_handlers_preserve_replacements_and_identity(synchronous):
    @dataclass(frozen=True)
    class Value(events.AgentEvent):
        text: str

    selected = []
    seen = []
    borrowed = object()

    class SyncConsumer(MroDispatch):
        def handlers_for(self, value):
            selected.append(value)
            yield from super().handlers_for(value)

        @handles(Value)
        def specific(self, value, context):
            assert context is borrowed
            if value.text == "wrong":
                return events.Notice("wrong event")
            return replace(value, text="replacement")

        @handles(events.AgentEvent)
        def shared(self, value, context):
            assert context is borrowed
            seen.append(value.text)

    class AsyncConsumer(SyncConsumer):
        @handles(Value)
        async def specific(self, value, context):
            return super().specific(value, context)

        @handles(events.AgentEvent)
        async def shared(self, value, context):
            return super().shared(value, context)

    consumer = SyncConsumer() if synchronous else AsyncConsumer()
    value = Value("original")
    result = (consumer.dispatch_sync(value, borrowed) if synchronous
              else await consumer.dispatch(value, borrowed))
    assert selected == [value]
    assert seen == ["replacement"]
    assert result == Value("replacement")
    with pytest.raises(TypeError, match="preserve event identity"):
        if synchronous:
            consumer.dispatch_sync(Value("wrong"), borrowed)
        else:
            await consumer.dispatch(Value("wrong"), borrowed)


def test_payload_fields_and_frozen_multiple_inheritance():
    start = events.ToolStart("id", "read", "Read file", {"path": "file"})
    assert start.activity_state == ActivityState.WORKING
    assert start.activity_detail == "Read file"
    assert events.ActivityEvent in type(start).__mro__
    assert events.ToolEvent in type(start).__mro__
    for value in (start, events.InputStarted(None), events.ModelChanged("id", True)):
        with pytest.raises(FrozenInstanceError):
            value.extra = "not a declared field"
    done = events.Done("failed", False, "input_authority_changed", {"elapsed_ms": 3})
    assert done.reason_code == "input_authority_changed"
    assert done.diagnostic == {"elapsed_ms": 3}


@dataclass(frozen=True)
class ContextWarning(events.ActivityEvent):
    """New behavior-bearing case; neither consumer is edited to support it."""

    text: str

    @property
    def activity_state(self):
        return ActivityState.WORKING

    @property
    def activity_detail(self):
        return self.text


async def test_new_activity_declaration_reaches_real_turn_consumer(comms, tmp_path, monkeypatch):
    root_id = comms.messaging.initialize_private_initial_protocol()
    owner = CommsAgent(
        comms,
        agent_bin="unused",
        private_nk_native_package=tmp_path,
        private_nk_wire_root_id=root_id,
    )
    session = await owner.new_session(cwd=str(tmp_path), mcp_servers=[])
    observed = []

    async def stream(*args, **kwargs):
        yield ContextWarning("context warning")
        observed.append(
            comms.agents.activity_of(owner.sessions.bindings[session.session_id]).detail
        )
        yield events.Done("done", True)

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", stream)
    try:
        await owner.turns.run_agent_turn(
            session.session_id, owner.sessions.bindings[session.session_id], "task"
        )
        assert observed == ["context warning"]
    finally:
        await owner.shutdown()


async def test_request_correlation_separates_families_and_ignores_late_results():
    pending = PendingRequests()
    model = pending.add(events.ModelChanged, "same")
    thinking = pending.add(events.ThinkingChanged, "same")
    with pytest.raises(ValueError):
        pending.add(events.ModelChanged, "same")
    pending.resolve(events.ModelChanged("same", False, "unavailable"))
    with pytest.raises(RuntimeError, match="unavailable"):
        await model
    assert not thinking.done()
    pending.resolve(events.ThinkingChanged("same", True))
    assert await thinking is None
    pending.resolve(events.ThinkingChanged("same", False, "late"))
    pending.discard(events.ThinkingChanged, "same")
    pending.resolve(events.ThinkingChanged("same", True))
    cancelled = pending.add(events.ModelChanged, "cancelled")
    cancelled.cancel()
    pending.resolve(events.ModelChanged("cancelled", True))


@pytest.mark.parametrize("publication_fails", [False, True])
async def test_settle_turn_releases_fence_after_publication_even_on_error(
    comms, tmp_path, monkeypatch, publication_fails
):
    comms.registry.declare(
        Thread(
            name="bot",
            tags=frozenset(),
            worktree=str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    owner = CommsAgent(comms, agent_bin="unused")
    lease = comms.agents.begin_turn("bot", "turn").turn_lease
    owner.sessions.bindings["session"] = "bot"
    task = asyncio.current_task()
    owner.turns.turn_tasks["session"] = task
    effects = []

    async def emit(session, event):
        assert comms.registry.require("bot").active_turn is None
        assert "session" not in owner.turns.turn_tasks
        assert isinstance(event, TurnTranscriptUpdate)
        assert not event.state.busy
        assert event.state.finished_turn_id == "turn"
        effects.append("publish")
        if publication_fails:
            raise RuntimeError("client closed")

    def release(fence):
        assert fence.turn_id == "turn"
        effects.append("release")

    monkeypatch.setattr(owner, "_emit_event", emit)
    monkeypatch.setattr(comms.goals, "release_waits_after_terminal_turn", release)
    if publication_fails:
        with pytest.raises(RuntimeError, match="client closed"):
            await owner.turns.settle_turn("session", "bot", "turn", lease, task=task)
    else:
        await owner.turns.settle_turn("session", "bot", "turn", lease, task=task)
    assert effects == ["publish", "release"]


async def test_stale_settlement_preserves_replacement_turn(
    comms, tmp_path, monkeypatch
):
    comms.registry.declare(
        Thread(
            name="bot",
            tags=frozenset(),
            worktree=str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    owner = CommsAgent(comms, agent_bin="unused")
    lease = comms.agents.begin_turn("bot", "turn").turn_lease
    owner.sessions.bindings["session"] = "bot"
    released = []
    monkeypatch.setattr(comms.goals, "release_waits_after_terminal_turn", released.append)
    comms.agents.finish_turn(lease)
    assert released == []
    replacement = comms.agents.begin_turn("bot", "turn").turn_lease
    observed = []

    async def emit(session, event):
        observed.append(event.state)

    monkeypatch.setattr(owner, "_emit_event", emit)
    await owner.turns.settle_turn("session", "bot", "turn", lease)
    assert released == [None]
    assert comms.registry.require("bot").turn_lease == replacement
    assert observed[0].active.turn_generation == replacement.identity.generation


@pytest.mark.parametrize("acquiring", [False, True])
async def test_cancelled_worker_keeps_original_lease_and_waiter_cleanup(
    comms, tmp_path, monkeypatch, acquiring,
):
    """Cancel after the real CAS, before its result crosses back to the loop."""
    comms.registry.declare(Thread("bot", frozenset(), str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid())))
    owner = CommsAgent(comms, agent_bin="unused")
    owner.sessions.bindings["session"] = "bot"
    committed, deliver = threading.Event(), threading.Event()
    original = comms.agents.begin_turn if acquiring else comms.agents.finish_turn
    release = comms.goals.release_waits_after_terminal_turn
    fences, publications = [], []
    loop_thread = threading.get_ident()

    def hold(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        result = original(*args, **kwargs)
        committed.set()
        assert deliver.wait(5), "test did not release its owned worker"
        return result

    def release_waiters(fence):
        assert threading.get_ident() != loop_thread
        fences.append(fence)
        return release(fence)

    async def emit(session, event):
        publications.append(event)

    monkeypatch.setattr(comms.goals, "release_waits_after_terminal_turn", release_waiters)
    monkeypatch.setattr(owner, "_emit_event", emit)
    if acquiring:
        monkeypatch.setattr(comms.agents, "begin_turn", hold)

        async def run():
            async with AsyncExitStack() as resources:
                await Coordination.run_worker(partial(owner.turns.acquire_turn,
                    resources, "session", "bot", "turn", "work"))
    else:
        lease = comms.agents.begin_turn("bot", "turn").turn_lease
        monkeypatch.setattr(comms.agents, "finish_turn", hold)

        async def run():
            await owner.turns.settle_turn("session", "bot", "turn", lease)

    pending = asyncio.create_task(run())
    try:
        assert await asyncio.to_thread(committed.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done(), "cancellation must join the owned CAS worker"
    finally:
        deliver.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert comms.registry.require("bot").active_turn is None
    assert comms.registry.require("bot").last_finished_turn_id == "turn"
    assert len(fences) == 1 and fences[0].turn_id == "turn"
    assert len(publications) == int(acquiring)


def test_internal_event_consumers_do_not_recover_string_tags():
    root = Path(__file__).parents[1] / "src" / "agent_comms"
    for filename, method in (
        ("owned_turn.py", "stream"),
        ("turn_progress.py", "consume"),
    ):
        tree = ast.parse((root / filename).read_text())
        function = next(
            n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == method
        )
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert not (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "event"
                    and node.func.attr == "get"
                )
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                assert node.value.id != "event"
