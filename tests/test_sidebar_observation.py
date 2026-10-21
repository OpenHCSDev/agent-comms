"""Sidebar states answer the same questions before and after a real read."""

import pickle
from dataclasses import fields, replace

import pytest

from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.ui_model.sidebar import SidebarModel
from agent_comms.ui_model.unread import ExactUnread, UnobservedUnread




def test_sidebar_apply_and_failure_publish_their_original_answers(comms):
    scheduled = []
    model = SidebarModel(scheduled.append)
    assert model.observation.channel_unread("#any") == UnobservedUnread()
    snapshot = comms.views.viewer_snapshot(str(comms.root.parent))
    model.apply(snapshot, SidebarModel.derive(snapshot))
    assert model.observation.require_snapshot() is snapshot
    assert model.observation.channel_order() is snapshot.channel_order
    assert model.observation.channel_unread("#any") == ExactUnread(0)
    with pytest.raises(KeyError):
        model.observation.bus_unread("missing")
    assert set(model.channels.rows) == set(snapshot.channels)
    failure = ValueError("original sidebar read failure")
    model.clear(failure)
    assert not model.channels.rows and not model.threads.rows
    assert model.observation.channel_unread("#any") == UnobservedUnread()
    with pytest.raises(ValueError) as caught:
        model.observation.require_snapshot()
    assert caught.value is failure


@pytest.fixture
def declare_reference(comms):
    def declare(name="reference", status=StoppedThreadStatus()):
        return comms.registry.declare(comms.registry.new_thread(
            name=name, tags=frozenset(), worktree=str(comms.root.parent),
        ), status=status)
    return declare


def test_registry_snapshot_constructor_and_pickle_own_immutable_maps(comms, declare_reference):
    original = declare_reference()
    captured = comms.registry.snapshot()
    supplied = dict(captured.threads)
    snapshot = replace(captured, threads=supplied)
    supplied.clear()
    for restored in (snapshot, pickle.loads(pickle.dumps(snapshot, protocol=5))):
        assert restored.threads[original.name] == original
        assert restored.by_lookup[original.incarnation.require_recorded().lookup] == original
        for declaration in fields(restored):
            with pytest.raises(TypeError):
                getattr(restored, declaration.name)["unowned"] = None


def test_current_reference_resolves_original_birth_after_registry_rename(comms, declare_reference):
    original = declare_reference(status=RunningThreadStatus())
    comms.registry.rename(original.name, "renamed")
    snapshot = comms.views.viewer_snapshot(str(comms.root.parent))
    references = pickle.loads(pickle.dumps(snapshot, protocol=5)).references
    current = references.thread(original.incarnation)
    assert current.name == "renamed"
    assert current.incarnation == original.incarnation
    assert current.current_in(references)


def test_recreated_thread_reference_resolves_new_birth_not_same_name(comms, declare_reference):
    original = declare_reference()
    before = comms.views.viewer_snapshot(str(comms.root.parent)).references
    comms.threads.delete(original.name)
    recreated = declare_reference(original.name)
    snapshot = comms.views.viewer_snapshot(str(comms.root.parent))
    references = pickle.loads(pickle.dumps(snapshot, protocol=5)).references
    assert recreated.incarnation != original.incarnation
    assert references.thread(recreated.incarnation).incarnation == recreated.incarnation
    assert before.thread(original.incarnation).incarnation == original.incarnation
    with pytest.raises(KeyError):
        references.thread(original.incarnation)
    with pytest.raises(TypeError):
        references.registry.by_lookup[original.incarnation.require_recorded().lookup] = original
