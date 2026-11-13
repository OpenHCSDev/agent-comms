"""The model menu and the coordination footer give one answer for a running thread."""

import asyncio
from dataclasses import dataclass
from typing import Literal

import pytest

from agent_comms.cli_verbs.new import NewThreadCliCommand
from agent_comms.cli_verbs.target_fields import TargetField
from agent_comms.execution.acp import CommsAgent
from agent_comms.agent_events import NativeMetadataObservation
from agent_comms.config_options import CatalogChoice, ModelConfigOption
from agent_comms.execution.native_arguments import NativeArguments
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_configuration import ThreadConfiguration
from agent_comms.registry.thread_model import (
    DeclaredModel,
    ThreadModel,
)
from agent_comms.registry.threads import ModelChoice, NativeThread
from conftest import installed_comms


@pytest.mark.parametrize('wire,arguments', [
    ('test/one', ('--provider', 'test', '--model', 'one')),
    (None, ()),
])
def test_saved_models_and_requests_share_catalog_arguments_without_changing_wire(wire, arguments, tmp_path, monkeypatch):

    for family in (ThreadModel, ModelChoice):
        model = FieldCodec.decode(family, wire)
        assert model.otherwise(None) == wire
        assert model.native(NativeArguments(())).argv == arguments
        assert FieldCodec.encode(model) == wire

    offered = CatalogChoice(name='One', value='test/one')

    async def discovered(option, selection):
        return [offered]

    monkeypatch.setattr(ModelConfigOption, 'discover', discovered)
    comms = installed_comms(tmp_path / 'wire')
    command = NewThreadCliCommand(name='', project=Checkout(tmp_path),
                                model=FieldCodec.decode(ThreadModel, wire))
    parameter = next(item for item in command.editable_fields if item.name == 'model')
    prepared = asyncio.run(parameter.prepare(comms.configuration_catalogs, command))
    assert offered in prepared.options
    assert prepared.choice_value(prepared.editor_default) == wire
    editor_value = prepared.editor_value(offered.value)
    assert (offered.name, editor_value) in prepared.choices
    assert prepared.plan.editor_payload(editor_value) == offered.value
    assert FieldCodec.decode(ThreadModel, prepared.plan.editor_payload(editor_value)) == DeclaredModel(offered.value)
    transported = FieldCodec.decode(tuple[CatalogChoice, ...], FieldCodec.encode(prepared.offered()))
    assert prepared.with_offered(transported).options == prepared.options
    assert prepared.with_offered(transported).choices == prepared.choices

    @dataclass
    class FiniteChoice:
        value: Literal['one', 'two'] = 'one'

    plan, = FieldCodec.field_plans(FiniteChoice)
    finite = asyncio.run(TargetField.of(plan, 'one').prepare(comms.configuration_catalogs, command))
    assert tuple(label for label, value in finite.choices) == ('one', 'two')
    for choice in finite.options:
        encoded = finite.editor_value(choice.value)
        assert finite.choice_value(encoded) == choice.value
        assert finite.plan.editor_payload(encoded) in ('one', 'two')


def test_keep_request_preserves_the_existing_model_and_chosen_request_replaces_it(tmp_path):

    thread = NativeThread('worker', frozenset(), Checkout(tmp_path), model=DeclaredModel('test/one'))
    assert FieldCodec.decode(ModelChoice, None).configured(thread) is thread
    updated = FieldCodec.decode(ModelChoice, 'test/two').configured(thread)
    assert updated.model == DeclaredModel('test/two')
    assert thread.model == DeclaredModel('test/one')


def test_serving_root_reuses_catalog_across_read_loops_and_authentication_invalidates_it(tmp_path, monkeypatch):
    import asyncio
    from agent_comms.cli_verbs.new import NewThreadCliCommand
    from agent_comms.config_options import CatalogChoice

    comms = installed_comms(tmp_path / "wire")
    project = Checkout(tmp_path)
    selection, = NewThreadCliCommand.project_bindings(comms, project)
    discoveries = []
    fail_next = False

    async def discovered(option, asked):
        nonlocal fail_next
        discoveries.append(asked.project_path)
        if fail_next:
            fail_next = False
            raise RuntimeError("Discovery interrupted")
        return [CatalogChoice(name="One", value="test/one")]

    monkeypatch.setattr(ModelConfigOption, "discover", discovered)

    def read_catalog():
        return comms.configuration_catalogs.catalog_for(ModelConfigOption)

    first = read_catalog()
    assert asyncio.run(first.choices(selection)) == [CatalogChoice(name="One", value="test/one")]
    assert read_catalog() is first
    assert asyncio.run(read_catalog().choices(selection)) == [CatalogChoice(name="One", value="test/one")]
    assert discoveries == [project.path]
    auth_file = first.owner.launch.configuration.auth_file
    assert auth_file.is_relative_to(tmp_path), "The control may only change its private credentials file"
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text("{}")
    assert asyncio.run(read_catalog().choices(selection)) == [CatalogChoice(name="One", value="test/one")]
    assert discoveries == [project.path, project.path]
    fail_next = True
    auth_file.write_text('{"providers": {}}')
    with pytest.raises(RuntimeError, match="Discovery interrupted"):
        asyncio.run(read_catalog().choices(selection))
    assert asyncio.run(read_catalog().choices(selection)) == [CatalogChoice(name="One", value="test/one")]
    assert discoveries == [project.path] * 4


@pytest.mark.asyncio
async def test_menu_reports_the_model_the_runtime_runs(tmp_path, monkeypatch):
    # This declared model catalogue avoids SDK/model discovery; no model runs.
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/one,test/two")
    comms = installed_comms(tmp_path / "w")
    owner = CommsAgent(comms, auto_wake=False)
    try:
        project = tmp_path / "alpha"
        project.mkdir()
        name = owner.sessions.declare_thread(str(project)).name
        reported = NativeMetadataObservation(model=DeclaredModel("test/two"))
        comms.agents.observe_runtime_info(name, reported)
        config = owner.sessions.config
        selection = comms.threads.running_selection(name)
        menu = await config.describe(owner.inputs.configuration.catalog_for(ModelConfigOption), selection)
        coordination = ThreadConfiguration(
            comms.root, comms.registry, comms.threads, comms.agents,
        ).answer(name)
        assert comms.registry.require(name).model.otherwise(None) is None
        assert menu.current_value == coordination.model == "test/two"
    finally:
        await owner.shutdown()


@pytest.mark.asyncio
async def test_menu_offers_the_catalog_when_the_running_model_left_it(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/one,test/two")
    comms = installed_comms(tmp_path / "w")
    owner = CommsAgent(comms, auto_wake=False)
    try:
        project = tmp_path / "alpha"
        project.mkdir()
        name = owner.sessions.declare_thread(str(project)).name
        comms.agents.observe_runtime_info(
            name, NativeMetadataObservation(model=DeclaredModel("gone/model")))
        menu = await owner.sessions.config.describe(
            owner.inputs.configuration.catalog_for(ModelConfigOption),
            comms.threads.running_selection(name),
        )
        assert menu.current_value == "gone/model"
        assert [(choice.name, choice.value) for choice in menu.options] == [
            ("gone/model (unavailable)", "gone/model"),
            ("test/one", "test/one"),
            ("test/two", "test/two"),
        ]
    finally:
        await owner.shutdown()
