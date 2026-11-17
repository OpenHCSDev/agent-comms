"""The configure form discovers from its thread while retaining keep-current choices."""

import socket

import pytest

from agent_comms.cli_verbs.configure import ConfigureCliCommand
from agent_comms.config_options import CatalogChoice, ModelConfigOption, ThinkingLevelConfigOption
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_model import DeclaredModel
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.transport.tools.sidebar import CommsPrepareTargetActionTool
from agent_comms.ui_model.observation import (
    CoreReadAnswered, CoreReadOperation, ObservationService, ReadNotifications,
    ServiceEnd, ViewerEnd,
)
from agent_comms.messaging.notification_reads import LiveNotificationRead
from observation_pass_fixture import watched_pass


@pytest.mark.parametrize("thread_type", [NativeThread, ExternalThread])
async def test_configure_form_keeps_its_thread_context_and_observer(comms, tmp_path, monkeypatch, thread_type):
    project = Checkout(tmp_path)
    thread = thread_type("settings-subject", frozenset(), project, model=DeclaredModel("test/old"))
    comms.registry.declare(thread)
    original = comms.registry.require(thread.name)

    async def models(option, selection):
        assert selection.project_path == project.path
        assert selection.model == original.model
        return [CatalogChoice("Old", "test/old"), CatalogChoice("New", "test/new")]

    async def thinking(option, selection):
        assert selection.project_path == project.path
        assert selection.model == original.model
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
            assert {field.name for field in prepared.editable_fields} == {"model", "thinking_level", "service_tier"}
            for field in prepared.fields:
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
