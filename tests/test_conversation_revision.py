"""A conversation revision tracks its selected scope, not registry traffic."""

import pytest

from agent_comms.comms import wire
from agent_comms.historical_views import (
    ChannelDisplayHistory,
    ConversationObservation,
    DMDisplayHistory,
    DMDisplayIdentity,
)
from agent_comms.registry_store import RegistryStore
from conftest import publish_initial


def declared_wire(root):
    comms = wire(root)
    comms.registry.declare(comms.registry.new_thread(name='peer', tags=frozenset({'comms'}), worktree=str(root)))
    return comms


@pytest.mark.parametrize("view,target", [(DMDisplayHistory, "peer"), (ChannelDisplayHistory, "#comms")])
def test_unrelated_registration_does_not_replace_display(tmp_path, view, target):
    comms = declared_wire(tmp_path)
    before = view.revision(comms.views, target, ConversationObservation(), worktree=str(tmp_path))
    comms.registry.declare(comms.registry.new_thread(name='unrelated', tags=frozenset({'other'}), worktree=str(tmp_path)))
    after = view.revision(comms.views, target, before, worktree=str(tmp_path))

    assert before.stores.registry != after.stores.registry
    assert not after.display_changed_since(before)


def test_peer_rename_replaces_display_once(tmp_path):
    comms = declared_wire(tmp_path)
    before = DMDisplayHistory.revision(comms.views, "peer", ConversationObservation(), worktree=str(tmp_path))
    comms.registry.rename("peer", "renamed")
    after = DMDisplayHistory.revision(comms.views, "peer", before, worktree=str(tmp_path))
    settled = DMDisplayHistory.revision(comms.views, "peer", after, worktree=str(tmp_path))

    assert isinstance(after.selection, DMDisplayIdentity)
    assert after.selection.peer.name == "renamed"
    assert after.selection.scope.second_names == frozenset({"peer", "renamed"})
    assert after.display_changed_since(before)
    assert not settled.display_changed_since(after)
    assert settled.selection is after.selection


@pytest.mark.parametrize("view,target", [(DMDisplayHistory, "peer"), (ChannelDisplayHistory, "#comms")])
def test_bus_append_reuses_original_selection_without_snapshot(tmp_path, monkeypatch, view, target):
    comms = declared_wire(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    before = view.revision(comms.views, target, ConversationObservation(), worktree=str(tmp_path))
    publish_initial(comms, "peer", "#comms", "plain bus message")
    snapshots = []
    original = RegistryStore.read_snapshot_unlocked

    def counted(store):
        snapshots.append(store.path)
        return original(store)

    monkeypatch.setattr(RegistryStore, "read_snapshot_unlocked", counted)
    after = view.revision(comms.views, target, before, worktree=str(tmp_path))
    assert not snapshots
    assert after.selection is before.selection
    assert after.stores.bus_log != before.stores.bus_log
