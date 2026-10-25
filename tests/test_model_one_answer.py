"""The model menu and the coordination footer give one answer for a running thread."""

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.agent_events import NativeMetadataObservation
from agent_comms.config_options import ModelConfigOption
from agent_comms.thread_configuration import ThreadConfiguration
from agent_comms.thread_model import DeclaredModel
from conftest import installed_comms


@pytest.mark.asyncio
async def test_menu_reports_the_model_the_runtime_runs(tmp_path, monkeypatch):
    # This declared model catalogue avoids SDK/model discovery; no model runs.
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/one,test/two")
    comms = installed_comms(tmp_path / "w")
    owner = CommsAgent(comms, runtime_enabled=False, auto_wake=False)
    try:
        project = tmp_path / "alpha"
        project.mkdir()
        name = owner.sessions.declare_thread(str(project)).name
        reported = NativeMetadataObservation(model=DeclaredModel("test/two"))
        comms.agents.observe_runtime_info(name, reported)
        config = owner.sessions.config
        selection = comms.threads.running_selection(name)
        menu = await config.describe(config.catalog_for(ModelConfigOption), selection)
        coordination = ThreadConfiguration(
            comms.root, comms.registry, comms.threads, comms.agents,
        ).answer(name)
        assert comms.registry.require(name).model.otherwise(None) is None
        assert menu.current_value == coordination.model == "test/two"
    finally:
        await owner.shutdown()
