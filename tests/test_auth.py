import json
from pathlib import Path

import pytest
from acp.schema import SessionConfigSelectOption

import agent_comms.backend as backend
from agent_comms.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms.config_options import InvalidConfigChoice, ModelConfigOption
from agent_comms.login import run_login
from agent_comms.native_arguments import NativeArguments
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent


async def test_terminal_auth_is_only_advertised_when_supported(tmp_path):
    agent = CommsAgent(wire(tmp_path), agent_bin="pi")
    assert not (await agent.initialize(1, {})).auth_methods
    methods = (await agent.initialize(1, {"auth": {"terminal": True}})).auth_methods
    method, = methods
    assert method.id == "pi-login"
    assert method.model_dump(by_alias=True)["type"] == "terminal"
    assert method.args == ["--login"]


