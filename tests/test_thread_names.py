"""One registry answer turns wanted text into a free thread name."""
from pathlib import Path

from agent_comms.repo import Checkout
import os

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.threads import Thread


def _declare(comms, name, worktree):
    comms.registry.declare(Thread(
        name, frozenset(), Checkout(Path(worktree)), process_identity=ProcessIdentity.capture(os.getpid())))


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


def test_a_remote_ui_reads_the_registry_with_its_renames(tmp_path):
    """The registry Core answers survives the wire: a rename's alias still resolves remotely."""
    from agent_comms.core_access import LocalCoreAccess
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.tools import CommsRegistryTool, RegistryRead

    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    comms.registry.rename("worker", "renamed")
    answer = LocalCoreAccess(comms).call(CommsRegistryTool())
    received = FieldCodec.decode(RegistryRead, FieldCodec.encode(answer))
    assert received.snapshot == comms.registry.snapshot()
    assert received.snapshot.canonical_name("worker") == "renamed"
    # An unbound session resolves its project's thread from the received snapshot alone.
    assert received.snapshot.session_thread(tmp_path, "worker").name_or("worker") == "renamed"
    assert received.snapshot.session_thread(tmp_path / "elsewhere", "worker").name_or("kept") == "kept"
    assert comms.registry.session_thread(tmp_path, "worker").name_or("worker") == "renamed"


def test_a_ui_reads_whether_a_channel_takes_input_through_core(tmp_path):
    """A channel answers as its own family member across the codec, so a remote composer reads
    the same read_only a local one does."""
    from agent_comms.channels import Channel
    from agent_comms.core_access import LocalCoreAccess
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.tools import CommsChannelTool

    comms = wire(tmp_path / "wire")
    for name in ("#all", "#any"):
        answer = LocalCoreAccess(comms).call(CommsChannelTool(name=name))
        assert FieldCodec.decode(Channel, FieldCodec.encode(answer)) == answer
        assert answer.read_only is False
