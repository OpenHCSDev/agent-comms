"""Merge observation closes the real stored order, without a completion request."""
from agent_comms.repository_name import RepositoryName
from dataclasses import fields, replace
import json
import subprocess
import time

import pytest

from agent_comms.repo import Repo
from agent_comms.field_codec import FieldCodec
from agent_comms.plan_phases import RecordPlanePhase
from agent_comms.todos import (CheckedProblemOrder, DoneTodoState, MergedProblemOrder, OperationDelivery,
                               MergedPullRequest, PullRequests, UnorderedProblemOrder)
from agent_comms.tools import CommsOrderDoneTool, CommsOrderServeTool
from agent_comms.recorded_time import RecordedEventTime
from cutover.carry_merged_orders import MergedOrders
from test_commander_problem_orders import working
from test_order_progress import checkout, commit, deadline_lines, git


@pytest.mark.parametrize('delivered', (False, True))
@pytest.mark.parametrize('legacy_depth', (0, 1, 2))
def test_installed_merge_closes_pending_or_delivered_order_once(
        commander, commander_script, checkout, tmp_path, monkeypatch, capsys,
        delivered, legacy_depth):
    comms, caller = commander
    script, _ = commander_script
    todo, unit = checkout
    if delivered:
        CommsOrderDoneTool(problem=todo.id, order_id=todo.order.id,
                          delivery=OperationDelivery(at=RecordedEventTime(500)),
                          claims=working(comms)).apply(comms, caller)
        todo = comms.goals.todos.get(todo.id)
    CommsOrderServeTool(problem=todo.id, serves=RecordPlanePhase).apply(comms, caller)
    todo = comms.goals.todos.get(todo.id)
    original = todo.order
    commit(unit)
    git(unit, 'switch', '-q', 'main')
    git(unit, '-c', 'user.name=t', '-c', 'user.email=t@t', 'merge', '--no-ff',
        '-m', 'merge the order', 'fix/p1')
    sha = git(unit, 'rev-parse', 'HEAD')
    base = git(unit, 'rev-parse', 'HEAD^1')
    installed = tmp_path / 'installed'
    installed.mkdir()
    git(installed, 'init', '-q', '-b', 'main')
    git(installed, 'remote', 'add', 'origin', str(unit))
    git(installed, 'fetch', '-q', 'origin', 'main')
    git(installed, 'checkout', '-q', '-B', 'main', base)
    repo = Repo(checkout=installed, github=todo.repo)
    actor = comms.registry.require(caller.name).incarnation
    merge = MergedPullRequest(1, 'fix/p1', sha, merge=sha, merged_at=500,
                              updated_at=500, orders=(original.id,))
    monkeypatch.setattr(PullRequests, 'read', lambda self, repo, state, order:
                        (merge,) if state == 'merged' else ())

    # The object being available locally is not installation: HEAD must contain it.
    assert not repo.contains(sha)
    comms.goals.todos.reconcile_merges(actor, (repo,), comms.registry.storage_rule)
    assert script.held_orders() == (todo,)
    assert comms.goals.todos.get(todo.id) == todo
    if not delivered:
        assert 'waiting on install' in deadline_lines(script)[0]

    git(installed, 'checkout', '-q', '-B', 'main', sha)
    assert repo.contains(sha)
    comms.goals.todos.reconcile_merges(actor, (repo,), comms.registry.storage_rule)
    assert script.held_orders() == ()
    closed = comms.goals.todos.get(todo.id)
    assert closed.state is DoneTodoState and closed.assignment is None
    assert closed.revision == todo.revision + 1 and not closed.pending
    assert isinstance(closed.order, MergedProblemOrder)
    assert closed.order.sha == sha
    assert all(getattr(closed.order, item.name) == getattr(original, item.name)
               for item in fields(CheckedProblemOrder))
    assert closed.order.id == original.id
    assert "original" not in FieldCodec.encode(closed.order)
    assert closed.order.phase is RecordPlanePhase
    assert RecordPlanePhase.serving((closed,)) == (closed,)
    assert closed.strikes == todo.strikes
    assert FieldCodec.decode(type(closed), FieldCodec.encode(closed)) == closed

    if legacy_depth:
        legacy = {'kind': 'merged', 'original': FieldCodec.encode(original), 'sha': sha}
        if legacy_depth == 2:
            closed = replace(closed, order=closed.order.folded('P2', 'Same root cause.'))
            legacy = {**FieldCodec.encode(closed.order), 'original': legacy}
        encoded = json.dumps(legacy)
        with comms.goals.todos.writing() as db:
            db.execute('UPDATE todo SET "order"=? WHERE id=?', (encoded, todo.id))
        assert MergedOrders.carry(comms.root, apply=False).changed == 1
        with comms.goals.todos.writing() as db:
            assert db.execute('SELECT "order" FROM todo WHERE id=?', (todo.id,)).fetchone()[0] == encoded
        assert MergedOrders.carry(comms.root, apply=True).changed == 1
        assert MergedOrders.carry(comms.root, apply=True).changed == 0
        assert comms.goals.todos.get(todo.id) == closed

    def no_child(*args, **kwargs):
        pytest.fail('A repeated closed-order read launched a subprocess')

    with monkeypatch.context() as repeat:
        repeat.setattr(subprocess, 'run', no_child)
        comms.goals.todos.reconcile_merges(actor, (repo,), comms.registry.storage_rule)
        assert script.held_orders() == ()
        assert comms.goals.todos.get(todo.id) == closed
    script.write_board()
    assert f'| {todo.id}:' not in (tmp_path / 'board.md').read_text()
    assert deadline_lines(script) == []
    assert comms.goals.todos.get(todo.id) == closed

    class TickComplete(Exception):
        pass

    def next_tick(seconds):
        raise TickComplete

    monkeypatch.setattr(time, 'sleep', next_tick)
    with pytest.raises(TickComplete):
        script.overdue()
    assert 'OVERDUE:' not in capsys.readouterr().out


def test_order_reads_stay_unchanged_during_install_until_one_overdue_pass(
        commander, commander_script, checkout, tmp_path, monkeypatch, capsys):
    import sys
    from agent_comms.commander.cli import main
    from agent_comms.maintenance_barrier import (
        InstallingPhase, MaintenanceBarrier, MaintenanceOperator, PausedPhase,
    )
    from agent_comms.todos import TodoStore

    comms, _ = commander
    script, _ = commander_script
    todo, unit = checkout
    commit(unit)
    git(unit, 'switch', '-q', 'main')
    git(unit, 'merge', '-q', '--ff-only', 'fix/p1')
    sha = git(unit, 'rev-parse', 'HEAD')
    repo = Repo(checkout=unit, github=todo.repo)
    script = script.model_copy(update={'repos': (repo,)})
    script.write(tmp_path)
    merge = MergedPullRequest(1, 'fix/p1', sha, merge=sha, merged_at=500,
                              updated_at=500, orders=(todo.order.id,))
    lookups = []

    def listed(self, repo, state, order):
        lookups.append(state)
        return (merge,) if state == 'merged' else ()

    def read_must_not_write(self):
        pytest.fail('Reading commander orders opened the order store for writing')

    monkeypatch.setattr(PullRequests, 'read', listed)
    barrier = MaintenanceBarrier(comms.registry.store.path)
    hold = barrier.begin(MaintenanceOperator.current())
    hold = barrier.advance(hold, PausedPhase)
    hold = barrier.advance(hold, InstallingPhase)
    try:
        with monkeypatch.context() as reading:
            reading.setattr(TodoStore, 'writing', read_must_not_write)
            assert script.todos() == (todo,)
            assert script.held_orders() == (todo,)
            assert lookups == [], 'An order read looked up pull requests'
            reading.setattr(sys, 'argv', [
                'agent-comms-commander', '--root', str(comms.root), 'order', 'board',
            ])
            main()
            assert f'| {todo.id}:' in capsys.readouterr().out
            assert comms.goals.todos.get(todo.id) == todo
    finally:
        barrier.clear(hold)

    class TickComplete(Exception):
        pass

    def next_tick(seconds):
        raise TickComplete

    lookups.clear()
    monkeypatch.setattr(time, 'sleep', next_tick)
    with pytest.raises(TickComplete):
        script.overdue()
    closed = comms.goals.todos.get(todo.id)
    assert lookups == ['merged'], 'Merge reconciliation did not run exactly once'
    assert closed.state is DoneTodoState and closed.assignment is None
    assert closed.revision == todo.revision + 1
    assert isinstance(closed.order, MergedProblemOrder)
    assert closed.order.sha == sha
    assert script.todos() == ()
    assert 'OVERDUE:' not in capsys.readouterr().out


def test_new_revision_is_not_closed_from_an_old_merge_observation(
        commander, commander_script, checkout, monkeypatch):
    comms, _ = commander
    script, installed = commander_script
    todo, _ = checkout
    repo = Repo(checkout=installed, github=todo.repo)
    merge = MergedPullRequest(1, 'fix/p1', 'a' * 40, merge='b' * 40, merged_at=500,
                              updated_at=500, orders=(todo.order.id,))
    monkeypatch.setattr(PullRequests, 'read', lambda self, repo, state, order: (merge,))
    newer = replace(todo, revision=todo.revision + 1,
                    order=replace(todo.order, id='replacement-order'))

    def observed(self, sha):
        with comms.goals.todos.writing() as db:
            newer.upsert(db)
        return True

    monkeypatch.setattr(Repo, 'contains', observed)
    comms.goals.todos.reconcile_merges(comms.registry.require('lead').incarnation,
                                      (repo,), comms.registry.storage_rule)
    assert comms.goals.todos.get(todo.id) == newer


def test_only_checked_orders_are_selected_for_merge_reconciliation(commander, checkout):
    comms, _ = commander
    todo, _ = checkout
    unordered = replace(todo, order=UnorderedProblemOrder())
    with comms.goals.todos.writing() as db:
        unordered.upsert(db)
    comms.goals.todos.reconcile_merges(comms.registry.require('lead').incarnation,
                                      (), comms.registry.storage_rule)
    assert comms.goals.todos.get(todo.id) == unordered


def test_failed_git_is_not_a_negative_ancestry_result(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        Repo(checkout=tmp_path / 'missing', github=RepositoryName.from_wire('test/repo')).contains('a' * 40)
