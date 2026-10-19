"""Real gated launches and durable runtime-prefix retirement."""
import os
import sys

import pytest

from agent_comms.child_process import AttachedChild, ChildRetirementFailed, ParentedProcess
from agent_comms.launch_runtime import LaunchRuntime, ThreadLaunch
from agent_comms.runtime_children import RuntimeChildrenStore


@pytest.fixture(autouse=True)
def calling_thread(monkeypatch, tmp_path):
    monkeypatch.setattr(ThreadLaunch, 'current', classmethod(
        lambda cls: ThreadLaunch('runtime-child-test', str(tmp_path))
    ))


def runtime_environment(prefix):
    (prefix / 'bin').mkdir(parents=True)
    interpreter = prefix / 'bin/python'
    interpreter.symlink_to(sys.executable)
    environment = ThreadLaunch('runtime-child-test', str(prefix)).apply(dict(os.environ))
    # This inherited value must never determine where a child is registered.
    environment['VIRTUAL_ENV'] = str(prefix / 'wrong-prefix')
    return environment, LaunchRuntime(interpreter)


def test_retirement_preserves_another_prefix_and_stops_an_exited_leaders_descendant(tmp_path):
    prefix, other = tmp_path / 'runtime', tmp_path / 'other'
    environment, runtime = runtime_environment(prefix)
    other_environment, other_runtime = runtime_environment(other)
    acknowledgement = prefix / 'descendant'
    child = ParentedProcess.launch((str(runtime.interpreter), '-c',
        'import os,time,pathlib,sys; p=os.fork(); pathlib.Path(sys.argv[1]).write_text(str(p)) if p else time.sleep(60)',
        str(acknowledgement)), env=environment, runtime=runtime)
    sibling = ParentedProcess.launch((str(other_runtime.interpreter), '-c', 'import time;time.sleep(60)'),
                                     env=other_environment, runtime=other_runtime)
    try:
        child.reap()
        record, = RuntimeChildrenStore.at(prefix).read().records
        assert record.identity == child.identity
        assert record.initiator.thread == 'runtime-child-test'
        assert record.runtime_prefix == prefix
        assert record.members()
        assert not RuntimeChildrenStore.at(prefix / 'wrong-prefix').path.exists()
        RuntimeChildrenStore.retire(prefix)
        assert not record.members()
        assert sibling.alive()
        with pytest.raises(ChildRetirementFailed, match='retired runtime'):
            ParentedProcess.launch((str(runtime.interpreter), '-c', 'raise AssertionError("must not execute")'),
                                   env=environment, runtime=runtime)
    finally:
        sibling.close()
        child.close()


async def test_async_launch_registration_is_removed_only_after_group_retirement(tmp_path):
    prefix = tmp_path / 'runtime'
    environment, runtime = runtime_environment(prefix)
    child = await AttachedChild.start((str(runtime.interpreter), '-c', 'import time;time.sleep(60)'),
                                      env=environment, runtime=runtime)
    try:
        record, = RuntimeChildrenStore.at(prefix).read().records
        assert record.identity == child.identity
        assert record.members()
    finally:
        await child.stop()
    assert not RuntimeChildrenStore.at(prefix).read().records


def test_failed_exec_leaves_no_registration(tmp_path):
    prefix = tmp_path / 'runtime'
    environment, runtime = runtime_environment(prefix)
    with pytest.raises(FileNotFoundError):
        ParentedProcess.launch((str(prefix / 'missing-command'),), env=environment, runtime=runtime)
    assert not RuntimeChildrenStore.at(prefix).read().records
