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
from agent_comms.registry.thread_model import DeclaredModel, UndeclaredModel
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.transport.tools.sidebar import CommsPrepareTargetActionTool, CommsTargetActionsTool
from agent_comms.ui_model.observation import (
    CoreReadAnswered, CoreReadOperation, ObservationService, ReadNotifications,
    ServiceEnd, ViewerEnd,
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


def test_only_a_native_owner_is_offered_next_turn_settings(comms, tmp_path, monkeypatch):
    """Configure writes a NativeThread and discovers its native runtime's catalogs. An external
    CLI (a bridged Claude Code reporting "claude-opus-5-5", which no native provider/model
    spelling decodes) is not offered the form whose choices could only fail to load."""
    project = Checkout(tmp_path)
    native = comms.registry.declare(NativeThread(
        "native-subject", frozenset(), project, model=DeclaredModel("test/old")))
    cli = comms.registry.declare(ExternalThread("cli-subject", frozenset(), project))
    comms.agents.observe_runtime_info(cli.name, NativeMetadataObservation(model=DeclaredModel("claude-opus-5-5")))

    def offered(thread) -> list[str]:
        menu = ui_core_read(comms, CommsTargetActionsTool(
            project=project, targets=CommandTarget.arguments((thread.name,), project)))
        return [action.declaration.declared_name for action in menu.actions]

    assert "configure" in offered(native)
    assert "configure" not in offered(cli)
