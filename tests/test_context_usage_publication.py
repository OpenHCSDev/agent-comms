"""Neutral readings stay outside ACP; the ACP consumer owns usage publication."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from acp.schema import UsageUpdate
import pytest

from agent_comms.agent_event_updates import AcpEventConsumer
from agent_comms.agent_events import ContextInfo
from agent_comms.pi_counts import PiCount


@pytest.mark.parametrize("used,size", [(20, 100), (0, 100), (None, 100), (20, None), (None, None)])
async def test_context_usage_publication(used, size):
    client = SimpleNamespace(session_update=AsyncMock())
    consumer = AcpEventConsumer(SimpleNamespace(), "usage-session", client)
    await consumer.dispatch(ContextInfo(
        context_used=PiCount.from_wire(used), context_size=PiCount.from_wire(size),
    ))
    if used is None or size is None:
        client.session_update.assert_not_awaited()
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
