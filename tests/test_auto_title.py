import pytest

from agent_comms.acp import CommsAgent
from agent_comms.comms import wire


async def test_new_pi_thread_requests_one_concise_title(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    agent = CommsAgent(wire(tmp_path), agent_args=["--model", "test/model"])
    await agent.new_session("/tmp/project")
    assert agent._comms.registry.require("project").auto_title_pending
    monkeypatch.setenv("PI_AGENT_ID", "project")
    with pytest.raises(ValueError, match="concise"):
        agent._comms.threads.rename_self("hey-boss-can-you-help-me-with-this-entire-first-message")
    agent._comms.threads.rename_self("project-path-sync")
    thread = agent._comms.registry.require("project")
    assert thread.name == "project-path-sync" and not thread.auto_title_pending
    await agent.shutdown()
