"""A broken executable must never reach runtime pointer replacement."""

import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.active_route import LocalRoute
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.change_report import FileLine
from agent_comms.goals import OrderCheck
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.store_files import _store_lock
from agent_comms.todos import (Assignment, GoalRef, MechanicalOrderRun, NoGoalReference,
    OpenTodoState, PendingProblemOrder, Todo, TodoStore)
from cutover.carry import CarryLedger
from cutover.carry_check_revision import CheckRevisions
from tools.publish_runtime import RuntimePublication


@pytest.mark.parametrize("command", ["exit 17", "echo broken >&2; exit 0"])
def test_cli_failure_leaves_links_untouched(tmp_path, monkeypatch, command):
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = tmp_path / "broken-runtime"
    binary = runtime / "bin/agent-comms"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n" + command + "\n")
    binary.chmod(0o700)
    link = tmp_path / "command"
    link.symlink_to("previous-command")
    publication = RuntimePublication(runtime, {link: binary})
    current = StoppedOwnerInstallation.current_pointer()
    current.parent.mkdir(parents=True)
    current.symlink_to("previous-runtime")
    before = (link.lstat(), current.lstat())
    route = LocalRoute(tmp_path / "bus")

    with pytest.raises(RuntimeError, match="Runtime CLI refused publication"):
        publication.publish(route.root, ())

    assert link.readlink() == Path("previous-command")
    assert current.readlink() == Path("previous-runtime")
    assert (link.lstat(), current.lstat()) == before


def test_publication_ends_retired_runtime_processes_and_keeps_the_new_runtimes(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runtimes = {name: tmp_path / name for name in ("previous-runtime", "runtime")}
    for runtime in runtimes.values():
        (runtime / "bin").mkdir(parents=True)
        (runtime / "bin/python").symlink_to(sys.executable)
    current = StoppedOwnerInstallation.current_pointer()
    current.parent.mkdir(parents=True)
    current.symlink_to(runtimes["previous-runtime"])
    retired, kept = (subprocess.Popen([runtime / "bin/python", "-c", "import time; time.sleep(60)"])
                     for runtime in runtimes.values())
    root = tmp_path / 'bus'
    store = TodoStore(root)
    original = Todo(id='P1', repo='org/repo', text='Fix the original order', creator='lead',
        creator_created=1, state=OpenTodoState, revision=1, goal=GoalRef('lead', 1, 'request'),
        assignment=Assignment('worker', 2, 'lead', 1, 'generation'),
        order=PendingProblemOrder(id='original-order', owner=FileLine(__file__, 1),
            check=OrderCheck(str(root), 'git fetch origin main && git checkout old && echo 7'),
            edits=('Fix the problem',), banned=('No extra store',),
            report='Report the delivered commit', run=MechanicalOrderRun(),
            at=RecordedEventTime(10), first_call_deadline=RecordedEventTime(130)))
    imported = replace(original, id='P2', goal=NoGoalReference(),
                       order=replace(original.order, id='imported-order'))
    with store.writing() as db:
        original.insert(db)
        imported.insert(db)
    # A format ledger is not proof that orders imported afterwards were carried.
    CarryLedger(root).record(CheckRevisions.name())
    barrier = MaintenanceBarrier(root / 'registry.json')
    hold = barrier.begin(MaintenanceOperator('publication test'))
    try:
        with _store_lock(barrier.wire_path):
            ended = RuntimePublication(runtimes["runtime"].resolve(), {}).publish_links((), hold)
            with pytest.raises(ValueError, match="would retire the runtime it publishes"):
                RuntimePublication(runtimes["runtime"].resolve(), {}).publish_links((), hold)
        assert [process.identity.pid for process in ended] == [retired.pid]
        assert retired.wait(timeout=10) == -signal.SIGTERM
        assert kept.poll() is None and current.readlink() == runtimes["runtime"].resolve()
        for before in (original, imported):
            assert store.get(before.id) == replace(before, order=replace(before.order,
                check=replace(before.order.check, command='echo 7')))
        assert CheckRevisions.carry(root, apply=False).changed == 0
    finally:
        barrier.clear(hold)
        for child in (retired, kept):
            child.kill()
            child.wait()
