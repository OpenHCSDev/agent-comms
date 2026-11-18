"""comms_serve_model starts the model-agent bridge on the bus host, one per name.

The tool spawns the real `agent_comms.cli_bridges.model_agent_client` against an installed scratch
bus. The endpoint is never called: nothing messages the thread, and registration alone proves the
bridge came up on this bus.
"""

from __future__ import annotations

import signal
import time

import pytest

from agent_comms.comms import Comms
from agent_comms.transport.connector import ConnectorServeModelTool, ModelServed
from agent_comms_hooks import ToolRefusedError

from tests.conftest import declare_test_bus


def serve(comms: Comms, **changes: object) -> ModelServed:
    tool = ConnectorServeModelTool(name="rl2", endpoint="http://127.0.0.1:9/v1/chat/completions",
                                   model="rl2", channels="#model_test", **changes)
    result = tool.apply(comms, None)
    assert isinstance(result, ModelServed)
    return result


def registered(comms: Comms, name: str, seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if comms.registry.snapshot().taken(name):
            return True
        time.sleep(0.1)
    return False


def test_serve_starts_one_bridge_per_name_and_replace_swaps_it(comms: Comms) -> None:
    declare_test_bus(comms, channels=("model_test",))
    first = serve(comms)
    try:
        assert first.started and first.process.alive()
        assert registered(comms, "rl2"), open(first.log).read()
        assert "model_test" in comms.registry.require("rl2").tags

        kept = serve(comms)
        assert not kept.started and kept.process == first.process

        swapped = serve(comms, replace=True)
        assert swapped.started and swapped.process != first.process
        deadline = time.monotonic() + 10
        while first.process.alive() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not first.process.alive()
    finally:
        for result in (first, ):
            if result.process.alive():
                result.process.signal(signal.SIGTERM)
        latest = ConnectorServeModelTool.live_bridge(comms.root / "model-agents" / "rl2.json")
        if latest is not None:
            latest.signal(signal.SIGTERM)


def test_serve_refuses_a_non_http_endpoint(comms: Comms) -> None:
    with pytest.raises(ToolRefusedError):
        ConnectorServeModelTool(name="rl2", endpoint="file:///etc/passwd", model="rl2").apply(comms, None)
