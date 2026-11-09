"""A runtime copy preserves declarations but cannot retain the source's live processes."""

import os

from agent_comms.execution.child_process import NoProcess, ProcessIdentity
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registration import Registration
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import RegistryFile


def test_a_registry_copy_names_no_live_process(tmp_path):
    source = Registration(RegistryFile.path_in(tmp_path))
    running = NativeThread("running", frozenset(), Checkout(tmp_path),
                           process_identity=ProcessIdentity.capture(os.getpid()))
    source.register(running)
    source.register(NativeThread("stopped", frozenset(), Checkout(tmp_path)))
    before = source.snapshot()
    target = RegistryFile.path_in(tmp_path / "copy")
    RegistryFile.copy_group(source.store.path, target)
    copied = Registration(target)
    assert set(copied.all_threads()) == {"running", "stopped"}
    assert copied.require("running").process_identity == NoProcess()
    assert copied.require("stopped").process_identity == NoProcess()
    assert source.snapshot() == before
    assert source.require("running").process_identity == running.process_identity
