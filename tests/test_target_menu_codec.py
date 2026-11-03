"""A thread's context menu crosses the connector as its own records."""

from pathlib import Path

import pytest

from agent_comms.channel_targets import (
    ChannelSelection,
    CommandTarget,
    NoChannel,
    OneChannel,
    PerTargetChannels,
    SurroundingChannel,
)
from agent_comms.cli_command import PreparedTargetAction, TargetAction, TargetEdit, TargetSelection
from agent_comms.cli_verbs.archive import ArchiveCliCommand
from agent_comms.cli_verbs.stop import StopCliCommand
from agent_comms.config_options import CatalogChoice
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord
from agent_comms.repo import Checkout


def test_a_surrounding_channel_keeps_its_wire_form_as_a_member_of_every_selection():
    assert FieldCodec.encode(NoChannel(), SurroundingChannel) is None
    assert FieldCodec.encode(OneChannel("ops"), SurroundingChannel) == "ops"
    for value in (NoChannel(), OneChannel("ops"), PerTargetChannels({"worker": ("ops", "dev")})):
        assert FieldCodec.decode(ChannelSelection, FieldCodec.encode(value, ChannelSelection)) == value
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(SurroundingChannel, {"worker": ["ops"]})


def test_a_selection_and_its_edit_cross_as_themselves():
    project = Checkout(Path("/project"))
    for declaration in (StopCliCommand, ArchiveCliCommand):
        selection = declaration.selected_targets(CommandTarget.arguments(("worker",), project))
        assert FieldCodec.decode(TargetSelection, FieldCodec.encode(selection, TargetSelection)) == selection
        edit = TargetEdit(declaration, selection, {"name": "worker"}, False, NoChannel(), project=project)
        assert FieldCodec.decode(TargetEdit, FieldCodec.encode(edit, TargetEdit)) == edit


def test_a_prepared_action_offers_the_choices_it_was_prepared_with():
    action = TargetAction((StopCliCommand(name="worker"),), ("worker",), StopCliCommand, OneChannel("ops"))
    assert FieldCodec.decode(TargetAction, FieldCodec.encode(action, TargetAction)) == action
    offered = tuple((CatalogChoice("A", "a"),) for _ in action.editable_fields)
    prepared = PreparedTargetAction(action.bound, action.targets, action.declaration, action.channels,
                                    offered=offered)
    received = FieldCodec.decode(PreparedTargetAction, FieldCodec.encode(prepared, PreparedTargetAction))
    assert received == prepared
    fields = received.fields
    assert tuple(item.name for item in fields) == tuple(item.name for item in action.editable_fields)


def test_every_action_result_crosses_by_shape_and_keeps_its_cli_form():
    from agent_comms.channel_management import TagChangeResult
    from agent_comms.channels import Channel
    from agent_comms.cli_commands import (
        TagRenamedResult, TargetReadResult, ThreadArchivedResult, ThreadPinnedResult,
        ThreadStoppedResult, ThreadTagsResult, ViewDeletedResult,
    )
    from agent_comms.cli_command import TargetBatchResult
    from agent_comms.cli_result import TargetEditResult
    from agent_comms.owner_lifecycle import OwnerStartResult

    answer = TargetEditResult | Channel
    for result in (ThreadStoppedResult("w"), ThreadArchivedResult("w"),
                   ThreadTagsResult("w", frozenset({"a"})), ThreadPinnedResult("w", True),
                   TagRenamedResult("a", "b"), ViewDeletedResult("v"), TargetReadResult("w"),
                   OwnerStartResult("w", 12, True), TagChangeResult("a", False), TargetBatchResult(())):
        encoded = FieldCodec.encode(result, answer)
        assert encoded == result.to_wire()
        assert FieldCodec.decode(answer, encoded) == result


def test_a_ui_lists_and_applies_a_menu_action_through_core(tmp_path):
    """A channel's menu and its edit go through the same core.call a remote UI sends, and both
    answers arrive as themselves."""
    from agent_comms.channels import Channel
    from agent_comms.cli_result import TargetEditResult
    from agent_comms.cli_verbs.read_target import ReadTargetCliCommand
    from agent_comms.comms import wire
    from agent_comms.core_access import LocalCoreAccess
    from agent_comms.tools import CommsTargetActionsTool, CommsTargetEditTool, TargetActionsRead

    comms = wire(tmp_path / "wire")
    project = Checkout(tmp_path)
    targets = CommandTarget.arguments(("#all",), project)
    core = LocalCoreAccess(comms)

    menu = core.call(CommsTargetActionsTool(project=project, targets=targets))
    assert FieldCodec.decode(TargetActionsRead, FieldCodec.encode(menu)) == menu
    assert [action.declaration.declared_name for action in menu.actions] == ["read-target"]

    edit = TargetEdit(ReadTargetCliCommand, ReadTargetCliCommand.selected_targets(targets), {}, project=project)
    answer = core.call(CommsTargetEditTool(edit=edit))
    received = FieldCodec.decode(TargetEditResult | Channel, FieldCodec.encode(answer, TargetEditResult | Channel))
    assert received == answer
