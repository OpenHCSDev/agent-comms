"""The model menu and the coordination footer give one answer for a running thread."""

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.agent_events import NativeMetadataObservation
from agent_comms.config_options import ModelConfigOption
from agent_comms.thread_configuration import ThreadConfiguration
from agent_comms.thread_model import DeclaredModel
from conftest import installed_comms


@pytest.mark.parametrize('wire,arguments', [
    ('test/one', ('--provider', 'test', '--model', 'one')),
    (None, ()),
])
def test_saved_models_and_requests_share_catalog_arguments_without_changing_wire(wire, arguments):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.native_arguments import NativeArguments
    from agent_comms.thread_model import ThreadModel
    from agent_comms.threads import ModelChoice

    for family in (ThreadModel, ModelChoice):
        model = FieldCodec.decode(family, wire)
        assert model.otherwise(None) == wire
        assert model.native(NativeArguments(())).argv == arguments
        assert FieldCodec.encode(model) == wire


def test_keep_request_preserves_the_existing_model_and_chosen_request_replaces_it(tmp_path):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.threads import ModelChoice, Thread

    thread = Thread('worker', frozenset(), str(tmp_path), model=DeclaredModel('test/one'))
    assert FieldCodec.decode(ModelChoice, None).configured(thread) is thread
    updated = FieldCodec.decode(ModelChoice, 'test/two').configured(thread)
    assert updated.model == DeclaredModel('test/two')
    assert thread.model == DeclaredModel('test/one')


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
