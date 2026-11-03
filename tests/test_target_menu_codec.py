"""A thread's context menu crosses the connector as its own records."""

import asyncio
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
    fields = asyncio.run(received.prepare_fields())
    assert tuple(item.name for item in fields) == tuple(item.name for item in action.editable_fields)
