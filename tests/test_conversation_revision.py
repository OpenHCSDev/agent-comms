"""A conversation revision tracks its selected scope, not registry traffic."""

from agent_comms.repo import Checkout
import pytest

from agent_comms.comms import wire
from agent_comms.channel_targets import CommandTarget
from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import (
    ChannelDisplayHistory,
    ConversationObservation,
    DMDisplayHistory,
    DMDisplayIdentity,
)
from agent_comms.private_path import FileRevision
from bus_record_fixture import prepare_native_comms
from agent_comms.threads import Thread


def declared_wire(root):
    comms = prepare_native_comms(wire(root))
    comms.registry.declare(Thread(name='peer', tags=frozenset({'comms'}), worktree=Checkout(root)))
    return comms


@pytest.mark.parametrize("view,target", [(DMDisplayHistory, CommandTarget.named("peer")), (ChannelDisplayHistory, CommandTarget.named("#comms"))])
@pytest.mark.parametrize("archived", [False, True])
def test_revision_keeps_the_typed_history_catalogue_observation(tmp_path, view, target, archived):
    comms = declared_wire(tmp_path / "live")
    if archived:
        source = declared_wire(tmp_path / "original")
        comms.views.attach_history(source.root)
    observed = view.revision(
        comms.views, target, ConversationObservation(), worktree=str(comms.root),
    )
    assert observed.selection.history_revision == FileRevision.capture(comms.bus.history.path)
    assert FieldCodec.decode(type(observed.selection), FieldCodec.encode(observed.selection)) == observed.selection


@pytest.mark.parametrize("view,target", [(DMDisplayHistory, CommandTarget.named("peer")), (ChannelDisplayHistory, CommandTarget.named("#comms"))])
def test_unrelated_registration_does_not_replace_display(tmp_path, view, target):
    comms = declared_wire(tmp_path)
    before = view.revision(comms.views, target, ConversationObservation(), worktree=str(tmp_path))
    comms.registry.declare(Thread(name='unrelated', tags=frozenset({'other'}), worktree=Checkout(tmp_path)))
    after = view.revision(comms.views, target, before, worktree=str(tmp_path))

    assert before.stores.registry != after.stores.registry
    assert not after.display_changed_since(before)


def test_peer_rename_replaces_display_once(tmp_path):
    comms = declared_wire(tmp_path)
    target = CommandTarget.named("peer")
    before = DMDisplayHistory.revision(comms.views, target, ConversationObservation(), worktree=str(tmp_path))
    comms.registry.rename("peer", "renamed")
    after = DMDisplayHistory.revision(comms.views, target, before, worktree=str(tmp_path))
    settled = DMDisplayHistory.revision(comms.views, target, after, worktree=str(tmp_path))

    assert isinstance(after.selection, DMDisplayIdentity)
    assert after.selection.peer.name == "renamed"
    assert after.selection.scope.second_names == frozenset({"peer", "renamed"})
    assert after.display_changed_since(before)
    assert not settled.display_changed_since(after)
    assert settled.selection is after.selection


