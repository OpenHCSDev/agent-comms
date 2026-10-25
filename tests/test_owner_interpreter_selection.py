"""A selection binds each exact process to its own observed interpreter."""

from agent_comms.repo import Checkout
import os
import sys
from contextlib import ExitStack
from dataclasses import replace

import pytest

from agent_comms.child_process import SpawnedProcess
from agent_comms.errors import RelationViolationError
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.threads import Thread


def test_mixed_interpreters_are_captured_per_owner_and_changes_refused(tmp_path, thread_registration):
    with ExitStack() as children:
        threads = {}
        for name in ("older", "current"):
            python = tmp_path / name / "bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            environment = dict(os.environ, AGENT_COMMS_THREAD=name,
                               AGENT_COMMS_AGENT_BIN=str(python))
            child = SpawnedProcess.launch((str(python), "-c", "import time; time.sleep(60)"),
                                           env=environment)
            children.callback(child.close)
            thread = Thread(
                name, frozenset(), Checkout(tmp_path), process_identity=child.identity)
            thread_registration.declare(thread)
            threads[name] = thread
        snapshot = thread_registration.snapshot()
        selections = tuple(OwnerRestartSelection.capture(snapshot, name) for name in threads)
        assert len({selected.interpreter for selected in selections}) == 2
        for selected in selections:
            assert selected.interpreter == str(tmp_path / selected.name / "bin/python")
            launch = RetainedOwnerLaunch.capture(selected, snapshot)
            assert launch.interpreter == selected.interpreter
            with pytest.raises(RelationViolationError):
                RetainedOwnerLaunch.capture(replace(selected, interpreter="/wrong/python"), snapshot)
