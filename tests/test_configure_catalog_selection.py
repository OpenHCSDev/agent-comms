"""The configure form discovers from its thread while retaining keep-current choices."""

import socket

import pytest

from agent_comms.cli_verbs.configure import ConfigureCliCommand
from agent_comms.cli_verbs.target_fields import ChoiceTargetField
from agent_comms.config_options import CatalogChoice, ModelConfigOption, ThinkingLevelConfigOption
from agent_comms.execution.runtime_observations import NativeMetadataObservation
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_model import CliInboxThreadModel, DeclaredModel, UndeclaredModel
from agent_comms.registry.threads import ChosenModel, CliInboxModelChoice, ExternalThread, ForkThread, NativeThread
from agent_comms.transport.tools.sidebar import CommsPrepareTargetActionTool, CommsTargetActionsTool
from agent_comms.ui_model.observation import (
    CoreReadAnswered,
    CoreReadOperation,
    ReadNotifications,
    ViewerEnd,
)
from agent_comms.ui_model.observation_service import (
    ObservationService,
    ServiceEnd,
)
from agent_comms.messaging.notification_reads import LiveNotificationRead
from conftest import ui_core_read
from observation_pass_fixture import watched_pass


@pytest.mark.parametrize("reported", [False, True])
async def test_configure_form_keeps_its_thread_context_and_observer(comms, tmp_path, monkeypatch, reported):
    project = Checkout(tmp_path)
    model = DeclaredModel("test/old")
    thread = NativeThread("settings-subject", frozenset(), project, model=UndeclaredModel() if reported else model)
    comms.registry.declare(thread)
    if reported:
        comms.agents.observe_runtime_info(thread.name, NativeMetadataObservation(model=model))
    original = comms.registry.require(thread.name)

    async def models(option, selection):
        assert selection.project_path == project.path
        assert selection.model == model
        return [CatalogChoice("Old", "test/old"), CatalogChoice("New", "test/new")]

    async def thinking(option, selection):
        assert selection.project_path == project.path
        assert selection.model == model
        return [CatalogChoice("Default", None)]

    monkeypatch.setattr(ModelConfigOption, "discover", models)
    monkeypatch.setattr(ThinkingLevelConfigOption, "discover", thinking)
    action = ConfigureCliCommand(name=thread.name).describe(comms, CommandTarget.named(thread.name), project)
    service_socket, viewer_socket = socket.socketpair()
    with ServiceEnd(service_socket.detach()) as service_end, ViewerEnd(viewer_socket.detach()) as viewer:
        service = ObservationService(comms, service_end)
        try:
            viewer.send(CoreReadOperation(str(comms.root), CommsPrepareTargetActionTool(action=action), request_id=1))
            service.receive()
            await watched_pass(service)
            replies = []
            while viewer.poll():
                replies.append(viewer.recv())
            answer, = (reply for reply in replies if isinstance(reply, CoreReadAnswered))
            from agent_comms.cli_verbs.cli_command import PreparedTargetAction
            prepared = FieldCodec.decode(PreparedTargetAction, answer.wire)
            assert {field.name for field in prepared.editable_fields} == {
                "context_edit_policy", "max_context", "model", "thinking_level", "service_tier",
            }
            max_context = next(field for field in prepared.fields if field.name == "max_context")
            assert max_context.label == "Max context"
            assert max_context.editor_default == "null"
            for field in prepared.fields:
                if not isinstance(field, ChoiceTargetField):
                    continue
                assert field.choice_value(field.editor_default) is None
                assert None in {choice.value for choice in field.offered()}
            viewer.send(ReadNotifications(str(comms.root), LiveNotificationRead(()), request_id=2))
            service.receive()
            await watched_pass(service)
            assert not service.closed
            assert comms.registry.require(thread.name) == original
        finally:
            service.retire()
            comms.views.transcript_reads.close()


def test_configure_merges_cli_inbox_into_model_picker_and_removes_standalone_menu_verbs(
    comms, tmp_path, monkeypatch,
):
    """Both native Pi and independent CLI-inbox threads use Configure's model picker to switch
    between Pi models and CLI inbox; subagents do not, and cli-inbox/native-thread are no longer
    separate right-click menu actions."""
    project = Checkout(tmp_path)
    native = comms.registry.declare(NativeThread(
        "native-subject", frozenset(), project, model=DeclaredModel("test/old")))
    cli = comms.registry.declare(ExternalThread("cli-subject", frozenset(), project))
    comms.agents.observe_runtime_info(cli.name, NativeMetadataObservation(model=DeclaredModel("claude-opus-5-5")))
    comms.registry.register_child("cli-child", cli.name, ForkThread, comms.goals)
    child = comms.registry.require("cli-child")

    def offered(thread) -> list[str]:
        menu = ui_core_read(comms, CommsTargetActionsTool(
            project=project, targets=CommandTarget.arguments((thread.name,), project)))
        return [action.declaration.declared_name for action in menu.actions]

    native_menu = offered(native)
    cli_menu = offered(cli)
    assert "configure" in native_menu
    assert "configure" in cli_menu
    assert "cli-inbox" not in native_menu
    assert "native-thread" not in cli_menu
    assert "configure" not in offered(child)

    # Opening Configure on an external CLI reporting a non-Pi model sees CliInboxThreadModel
    # and skips Pi thinking-level discovery instead of failing.
    assert comms.threads.running_selection(cli.name).model == CliInboxThreadModel()

    async def fake_models(option, selection):
        return [
            CatalogChoice("Old", "test/old"),
            CatalogChoice("New", "test/new"),
            CatalogChoice("CLI inbox", "cli-inbox", "External CLI or MCP session on this thread's inbox"),
        ]

    thinking_calls = 0

    async def fake_thinking(option, selection):
        nonlocal thinking_calls
        if not selection.model.discovers_thinking_levels:
            return []
        thinking_calls += 1
        return [CatalogChoice("Default", None)]

    monkeypatch.setattr(ModelConfigOption, "discover", fake_models)
    monkeypatch.setattr(ThinkingLevelConfigOption, "discover", fake_thinking)

    # Switch Native Pi -> CLI inbox through Configure's model picker.
    to_cli = ConfigureCliCommand(name=native.name, model=CliInboxModelChoice()).apply(comms)
    assert to_cli.model == CliInboxThreadModel()
    assert isinstance(comms.registry.require(native.name), ExternalThread)
    assert thinking_calls == 0

    # Switch CLI inbox -> Native Pi with a chosen model through Configure's model picker.
    to_native = ConfigureCliCommand(name=cli.name, model=ChosenModel("test/new")).apply(comms)
    assert to_native.model == DeclaredModel("test/new")
    restored = comms.registry.require(cli.name)
    assert isinstance(restored, NativeThread)
    assert restored.model == DeclaredModel("test/new")

    # Round-trip FieldCodec decoding for "cli-inbox" in both ThreadModel and ModelChoice.
    assert FieldCodec.decode(CliInboxThreadModel, "cli-inbox") == CliInboxThreadModel()
    assert FieldCodec.decode(CliInboxModelChoice, "cli-inbox") == CliInboxModelChoice()
    assert FieldCodec.encode(CliInboxThreadModel()) == "cli-inbox"
    assert FieldCodec.encode(CliInboxModelChoice()) == "cli-inbox"

    # Creating a new thread with model=CliInboxThreadModel() declares an ExternalThread directly.
    from agent_comms.projects.checkouts import LaunchCheckout
    from agent_comms.registry.thread_management import NewThreadSpec
    from agent_comms.registry.thread_status import NoBridgeThreadStatus
    created = comms.threads.create(NewThreadSpec(
        name="fresh-cli",
        project=LaunchCheckout(tmp_path),
        model=CliInboxThreadModel(),
        thinking_level=native.thinking_level,
    ))
    assert isinstance(created, ExternalThread)
    assert isinstance(comms.registry.status("fresh-cli"), NoBridgeThreadStatus)
