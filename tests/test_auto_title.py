import pytest

from agent_comms.acp import CommsAgent

from agent_comms.tool_caller import ThreadCaller
from agent_comms.comms import Comms
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.active_route import resolve_comms_route
import dataclasses


async def test_new_pi_thread_requests_one_concise_title(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    selected_comms = Comms(resolve_comms_route(tmp_path), native=NativeThreadDefaults.from_runtime())
    agent = CommsAgent(selected_comms, native=dataclasses.replace(selected_comms.owners.native_defaults, arguments=tuple(["--model", "test/model"])))
    await agent.new_session("/tmp/project")
    assert agent._comms.registry.require("project").auto_title_pending
    monkeypatch.setenv("AGENT_COMMS_THREAD", "project")
    with pytest.raises(ValueError, match="concise"):
        agent._comms.threads.rename_self("hey-boss-can-you-help-me-with-this-entire-first-message", caller=ThreadCaller("project"))
    agent._comms.threads.rename_self("project-path-sync", caller=ThreadCaller("project"))
    thread = agent._comms.registry.require("project")
    assert thread.name == "project-path-sync" and not thread.auto_title_pending
    await agent.shutdown()
