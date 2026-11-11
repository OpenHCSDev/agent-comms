"""Project selection retains the caller's original registry identity."""
from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_text import GivenThreadText
from agent_comms.registry.threads import NativeThread


@pytest.fixture
def project_source(tmp_path):
    current, destination = tmp_path / "current", tmp_path / "destination"
    current.mkdir()
    destination.mkdir()
    comms = wire(tmp_path / "wire")
    original = comms.registry.declare(NativeThread("source", frozenset(), Checkout(current)))
    return comms, original, destination


def test_captured_project_target_follows_its_original_rename(project_source):
    comms, original, destination = project_source
    comms.registry.rename(original.name, "renamed")
    changed = comms.threads.set_project(original.incarnation, str(destination))
    current = comms.registry.require("renamed")
    assert changed.thread == current.name == "renamed"
    assert current.incarnation == original.incarnation
    assert current.worktree == Checkout(destination)
    assert current.earlier_projects == (original.worktree,)


def test_recreated_name_cannot_receive_the_original_project_change(project_source):
    comms, original, destination = project_source
    with comms.registry.store.editing() as edit:
        edit.document.remove(original.name)
        edit.commit()
    replacement = comms.registry.declare(NativeThread(
        original.name, frozenset(), original.worktree))
    assert replacement.incarnation != original.incarnation
    with pytest.raises(RelationViolationError):
        comms.threads.set_project(original.incarnation, str(destination))
    assert comms.registry.require(replacement.name) == replacement
    assert not comms.registry.require(replacement.name).earlier_projects


def test_same_birth_metadata_does_not_retire_a_project_target(project_source):
    comms, original, destination = project_source
    titled = comms.registry.declare(replace(original, title=GivenThreadText("new title")))
    changed = comms.threads.set_project(original.incarnation, str(destination))
    current = comms.registry.require(original.name)
    assert changed.changed and current.worktree == Checkout(destination)
    assert current.title == titled.title
    assert current.incarnation == original.incarnation
