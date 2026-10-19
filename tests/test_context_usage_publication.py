"""Neutral readings stay outside ACP; the ACP consumer owns usage publication."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from acp.schema import UsageUpdate
import pytest

from agent_comms.acp_extension import CoordinationChangedUpdate, decode_updates
from agent_comms.agent_event_updates import AcpEventConsumer
from agent_comms.agent_events import ContextInfo
from agent_comms.context_reading import UnmeasuredContext
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
    agent = SimpleNamespace(
        sessions=SimpleNamespace(require=Mock(return_value="usage-thread")),
        comms=SimpleNamespace(configuration=SimpleNamespace(answer=Mock(return_value=no_usage))),
    )
    consumer = AcpEventConsumer(agent, "usage-session", client)
    await consumer.dispatch(ContextInfo(
        context_used=PiCount.from_wire(used), context_size=PiCount.from_wire(size),
    ))
    if not reported:
        client.session_update.assert_awaited_once()
        update = client.session_update.await_args.kwargs["update"]
        assert decode_updates(update.field_meta) == (no_usage,)
        assert isinstance(decode_updates(update.field_meta)[0].context_usage, UnmeasuredContext)
    else:
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
