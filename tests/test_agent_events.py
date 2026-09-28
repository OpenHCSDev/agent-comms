"""Nominal extension, dispatch ordering, and shared settlement contracts."""

import ast
import os
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path

import pytest

from agent_comms import agent_events as events
from agent_comms.acp import CommsAgent
from agent_comms.activity import ActivityState
from agent_comms.agent_loop import ParticipantEventConsumer
from agent_comms.child_process import ProcessIdentity
from agent_comms.mro_dispatch import MroDispatch, handles
from agent_comms.pending_requests import PendingRequests
from agent_comms.threads import Thread


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


async def test_new_activity_declaration_reaches_both_real_consumers(comms, tmp_path, monkeypatch):
    comms.threads.register(
        Thread(
            name="bot",
            tags=frozenset(),
            worktree=str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    participant = ParticipantEventConsumer(comms, "bot", "task")
    await participant.dispatch(ContextWarning("context warning"))
    assert comms.agents.activity_of("bot").detail == "context warning"
    owner = CommsAgent(comms, agent_bin="unused")
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
    comms.threads.register(
        Thread(
            name="bot",
            tags=frozenset(),
            worktree=str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    owner = CommsAgent(comms, agent_bin="unused")
    lease = comms.agents.begin_turn("bot", "turn")
    owner.turns.active_turns["session"] = "turn"
    effects = []

    async def emit(session, event):
        assert comms.registry.require("bot").active_turn is None
        assert "session" not in owner.turns.active_turns
        assert event == events.TurnSettled("turn")
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
            await owner.turns.settle_turn("session", "bot", "turn", lease)
    else:
        await owner.turns.settle_turn("session", "bot", "turn", lease)
    assert effects == ["publish", "release"]


async def test_stream_settlement_defers_waiters_and_preserves_replacement_turn(
    comms, tmp_path, monkeypatch
):
    comms.threads.register(
        Thread(
            name="bot",
            tags=frozenset(),
            worktree=str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    owner = CommsAgent(comms, agent_bin="unused")
    lease = comms.agents.begin_turn("bot", "turn")
    owner.turns.active_turns["session"] = "turn"
    released = []
    monkeypatch.setattr(comms.goals, "release_waits_after_terminal_turn", released.append)
    fence = owner.turns.finish_turn_stream("session", "bot", "turn", lease)
    assert released == []
    owner.turns.active_turns["session"] = "replacement"
    await owner.turns.settle_turn(
        "session", "bot", "turn", lease, stream_settled=True, terminal_fence=fence
    )
    assert released == [fence]
    assert owner.turns.active_turns["session"] == "replacement"


def test_internal_event_consumers_do_not_recover_string_tags():
    root = Path(__file__).parents[1] / "src" / "agent_comms"
    for filename, method in (
        ("owned_turn.py", "stream"),
        ("turn_progress.py", "consume"),
        ("agent_loop.py", "_ask_agent"),
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
