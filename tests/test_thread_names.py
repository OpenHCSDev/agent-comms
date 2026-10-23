"""One registry answer turns wanted text into a free thread name."""

import os

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.threads import Thread


def _declare(comms, name, worktree):
    comms.registry.declare(Thread(
        name, frozenset(), worktree, process_identity=ProcessIdentity.capture(os.getpid())))


def test_wanted_text_gets_one_slug_and_the_first_free_suffix(tmp_path):
    comms = wire(tmp_path / "wire")
    names = comms.registry.snapshot()
    # Session folders and rename titles share one slug rule.
    assert names.claim_name("My project!") == "My-project"
    assert names.claim_name("_scratch_") == "_scratch_"
    assert names.claim_name("***") == "session"
    _declare(comms, "user", str(tmp_path))
    _declare(comms, "user-2", str(tmp_path))
    assert comms.registry.snapshot().claim_name("user") == "user-3"


def test_a_permanent_alias_is_reserved_except_for_its_own_thread(tmp_path):
    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    comms.registry.rename("worker", "renamed")
    names = comms.registry.snapshot()
    assert names.taken("worker") and names.taken("renamed")
    assert names.claim_name("worker") == "worker-2"
    # A rename back to one of the thread's own names keeps that name.
    assert names.claim_name("worker", owner="renamed") == "worker"
    assert names.claim_name("renamed", owner="renamed") == "renamed"


def test_rename_takes_its_own_alias_and_refuses_another_threads(tmp_path):
    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    _declare(comms, "other", str(tmp_path))
    comms.registry.rename("worker", "renamed")
    names = comms.registry.snapshot()
    assert names.taken("worker") and not names.taken("worker", owner="renamed")
    with pytest.raises(RelationViolationError):
        comms.threads._rename_thread("other", "worker")
    assert comms.threads._rename_thread("renamed", "worker").current == "worker"
    assert comms.registry.canonical_name("renamed") == "worker"
