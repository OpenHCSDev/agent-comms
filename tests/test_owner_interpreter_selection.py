"""A selection binds each exact process to its own observed interpreter."""

import os
import sys
from contextlib import ExitStack
from dataclasses import replace

import pytest

from agent_comms.child_process import ParentedProcess
from agent_comms.errors import RelationViolationError
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.owner_lifecycle import OwnerRestartSelection
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.thread_status import RunningThreadStatus


def test_mixed_interpreters_are_captured_per_owner_and_changes_refused(tmp_path, thread_registration):
    with ExitStack() as children:
        threads = {}
        for name in ("older", "current"):
            python = tmp_path / name / "bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            environment = dict(os.environ, AGENT_COMMS_THREAD=name,
                               AGENT_COMMS_AGENT_BIN=str(python))
            child = ParentedProcess.launch((str(python), "-c", "import time; time.sleep(60)"),
                                           env=environment)
            children.callback(child.close)
            threads[name] = thread_registration.new_thread(
                name, frozenset(), str(tmp_path), process_identity=child.identity, created_at=1.0)
        snapshot = RegistrySnapshot(
            threads=threads, statuses={name: RunningThreadStatus() for name in threads},
            last_seen={name: 1.0 for name in threads}, aliases={},
            owner_generations={name: 1 for name in threads},
            _claimed_generations={name: 1 for name in threads},
            by_lookup={thread.incarnation.lookup: thread for thread in threads.values()},
        )
        selections = tuple(OwnerRestartSelection.capture(snapshot, name) for name in threads)
        assert len({selected.interpreter for selected in selections}) == 2
        for selected in selections:
            assert selected.interpreter == str(tmp_path / selected.name / "bin/python")
            launch = RetainedOwnerLaunch.capture(selected, snapshot)
            assert launch.interpreter == selected.interpreter
            with pytest.raises(RelationViolationError):
                RetainedOwnerLaunch.capture(replace(selected, interpreter="/wrong/python"), snapshot)
