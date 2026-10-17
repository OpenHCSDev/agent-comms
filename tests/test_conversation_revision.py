"""A conversation revision tracks its selected scope, not registry traffic."""
from agent_comms.storage_rules import StorageRule

import pytest

from agent_comms.comms import wire
from agent_comms.historical_views import (
    ChannelDisplayHistory,
    DMDisplayHistory,
    DMSelection,
)
from agent_comms.threads import Thread


def declared_wire(root):
    comms = wire(root)
    comms.registry.declare(Thread(name="peer", tags=frozenset({"comms"}), worktree=str(root), storage_roots=StorageRule().initial_roots('peer', str(root))))
    return comms


@pytest.mark.parametrize("view,target", [(DMDisplayHistory, "peer"), (ChannelDisplayHistory, "#comms")])
def test_unrelated_registration_does_not_replace_display(tmp_path, view, target):
    comms = declared_wire(tmp_path)
    before = view.revision(comms.views, target, worktree=str(tmp_path))
    comms.registry.declare(Thread(name="unrelated", tags=frozenset({"other"}), worktree=str(tmp_path), storage_roots=StorageRule().initial_roots('unrelated', str(tmp_path))))
    after = view.revision(comms.views, target, worktree=str(tmp_path))

    assert before.stores.registry != after.stores.registry
    assert not after.display_changed_since(before)


def test_peer_rename_replaces_display_once(tmp_path):
    comms = declared_wire(tmp_path)
    before = DMDisplayHistory.revision(comms.views, "peer", worktree=str(tmp_path))
    comms.registry.rename("peer", "renamed")
    after = DMDisplayHistory.revision(comms.views, "peer", worktree=str(tmp_path))
    settled = DMDisplayHistory.revision(comms.views, "peer", worktree=str(tmp_path))

    assert isinstance(after.selection, DMSelection)
    assert after.selection.peer.name == "renamed"
    assert after.selection.scope.second_names == frozenset({"peer", "renamed"})
    assert after.display_changed_since(before)
    assert not settled.display_changed_since(after)
