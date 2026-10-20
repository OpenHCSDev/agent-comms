import pytest

from agent_comms.acp import CommsAgent
from agent_comms.tool_caller import ThreadCaller


async def test_new_pi_thread_requests_one_concise_title(private_comms, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    agent = CommsAgent(private_comms, agent_args=["--model", "test/model"])
    await agent.new_session("/tmp/project")
    assert agent._comms.registry.require("project").auto_title_pending
    monkeypatch.setenv("AGENT_COMMS_THREAD", "project")
    with pytest.raises(ValueError):
        agent._comms.threads.rename_self("hey-boss-can-you-help-me-with-this-entire-first-message", caller=ThreadCaller("project"))
    agent._comms.threads.rename_self("project-path-sync", caller=ThreadCaller("project"))
    thread = agent._comms.registry.require("project")
    assert thread.name == "project-path-sync" and not thread.auto_title_pending
    await agent.shutdown()
