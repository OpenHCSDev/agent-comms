"""Neutral readings stay outside ACP; the ACP consumer owns usage publication."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from acp.schema import UsageUpdate
import pytest

from agent_comms.acp_extension import (
    CoordinationChangedUpdate, CursorAdvancedUpdate, CursorEnvelope, EmptyCursorObservation,
    GoalChangedUpdate, NoCursorScope, NoQueueScope, PendingQueueProjection, QueueChangedUpdate,
    decode_updates,
)
from agent_comms.agent_event_updates import AcpEventConsumer
from agent_comms.agent_events import ContextInfo
from agent_comms.context_reading import UnmeasuredContext
from agent_comms.goal_presentation import GoalSnapshot, IdleGoalExecution
from agent_comms.goals import NoGoal
from agent_comms.session_lifecycle import SessionLifecycle
from agent_comms.pi_counts import PiCount
from agent_comms.thread_identity import RecordedThreadIncarnation


@pytest.mark.parametrize("used,size,reported", [
    (20, 100, True), (0, 100, False), (None, 100, False),
    (20, None, False), (None, None, False),
])
async def test_context_usage_publication(used, size, reported):
    client = SimpleNamespace(session_update=AsyncMock())
    no_usage = CoordinationChangedUpdate(
        RecordedThreadIncarnation("usage-thread", 1.0), "/private/wire", 123,
        "/project", "fixture-model", "high", "Usage", UnmeasuredContext(),
    )
    goal = GoalSnapshot(NoGoal(), IdleGoalExecution())
    queue = QueueChangedUpdate(NoQueueScope(), 1, PendingQueueProjection())
    cursor = CursorAdvancedUpdate(CursorEnvelope(NoCursorScope(), 1, EmptyCursorObservation()))
    # Exercise the actual publication, metadata and configuration capture methods;
    # only their external stores, queue/cursor providers and transport are controlled.
    sessions = object.__new__(SessionLifecycle)
    sessions.bindings = {"usage-session": "usage-thread"}
    sessions.runtime = client
    sessions.comms = SimpleNamespace(
        configuration=SimpleNamespace(answer=Mock(return_value=no_usage)),
        goals=SimpleNamespace(goal_snapshot=Mock(return_value=goal)),
    )
    sessions.effects = SimpleNamespace(
        inputs=SimpleNamespace(queue_state=AsyncMock(return_value=queue)),
        cursors=SimpleNamespace(trusted_metadata=AsyncMock(return_value=(cursor,))),
    )
    sessions.publish_metadata = AsyncMock(wraps=sessions.publish_metadata)
    agent = SimpleNamespace(sessions=sessions)
    consumer = AcpEventConsumer(agent, "usage-session", client)
    await consumer.dispatch(ContextInfo(
        context_used=PiCount.from_wire(used), context_size=PiCount.from_wire(size),
    ))
    if not reported:
        client.session_update.assert_awaited_once()
        update = client.session_update.await_args.kwargs["update"]
        sessions.publish_metadata.assert_awaited_once_with("usage-session")
        assert decode_updates(update.field_meta) == (
            no_usage, GoalChangedUpdate.from_snapshot(goal), queue, cursor,
        )
        assert isinstance(decode_updates(update.field_meta)[0].context_usage, UnmeasuredContext)
        sessions.comms.configuration.answer.assert_called_once_with("usage-thread")
        sessions.effects.inputs.queue_state.assert_awaited_once_with("usage-session")
        sessions.effects.cursors.trusted_metadata.assert_awaited_once_with("usage-thread", "usage-session")
    else:
        sessions.publish_metadata.assert_not_awaited()
        sessions.comms.configuration.answer.assert_not_called()
        client.session_update.assert_awaited_once_with(
            session_id="usage-session",
            update=UsageUpdate(session_update="usage_update", used=used, size=size),
        )


def test_native_launcher_imports_no_acp():
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("AGENT_COMMS_", "PI_"))}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import agent_comms.native_pi; "
         "import agent_comms.active_route; "
         "assert not any(name == 'acp' or name.startswith('acp.') for name in sys.modules)"],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
