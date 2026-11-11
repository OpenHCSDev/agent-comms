"""A live process that serves no owner runtime socket is an unreachable owner, which the
connector answers as a refusal. FileNotFoundError once escaped as HTTP 500, and a remote Toad's
context bar raised WorkerFailed toasts for an external thread whose registered process is its
bridge, not a runtime host."""

import os

import pytest

from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.transport.owner_connection import OwnerConnection


def test_a_live_process_without_a_runtime_socket_is_an_unreachable_owner(tmp_path):
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    StorageRule().prepare_isolated(root)
    (root / "runtime").mkdir(mode=0o700, exist_ok=True)
    alive = ProcessIdentity.capture(os.getpid())
    with pytest.raises(ConnectionError, match="serves no owner runtime socket"):
        with OwnerConnection.dispatching(root, alive, None):
            pass
