"""Merge observation closes the real stored order, without a completion request."""
from dataclasses import replace
import subprocess

import pytest

from agent_comms.repo import Repo
from agent_comms.field_codec import FieldCodec
from agent_comms.todos import (DoneTodoState, MergedProblemOrder, OperationDelivery,
                               MergedPullRequest, PullRequests, UnorderedProblemOrder)
from agent_comms.tools import CommsOrderDoneTool
from test_commander_problem_orders import working
from test_order_progress import checkout, commit, deadline_lines, git


@pytest.mark.parametrize('delivered', (False, True))
def test_installed_merge_closes_pending_or_delivered_order_once(
        commander, commander_script, checkout, tmp_path, monkeypatch, capsys, delivered):
    comms, caller = commander
    script, _ = commander_script
    todo, unit = checkout
    if delivered:
        CommsOrderDoneTool(problem=todo.id, order_id=todo.order.id,
                          delivery=OperationDelivery(), claims=working(comms)).apply(comms, caller)
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
    config = script.CONFIG.model_copy(update={'repos': (repo,)})
    monkeypatch.setattr(script, 'CONFIG', config)
    merge = MergedPullRequest(1, 'fix/p1', sha, merge=sha, merged_at=500,
                              updated_at=500, orders=(original.id,))
    monkeypatch.setattr(PullRequests, 'read', lambda self, repo, state, order:
                        (merge,) if state == 'merged' else ())

    # The object being available locally is not installation: HEAD must contain it.
    assert not repo.contains(sha)
    assert script.COMMANDER.orders(script.CONFIG.repos) == (todo,)
    assert comms.goals.todos.get(todo.id) == todo
    if not delivered:
        assert 'waiting on install' in deadline_lines(script)[0]

    git(installed, 'checkout', '-q', '-B', 'main', sha)
    assert repo.contains(sha)
    assert script.COMMANDER.orders(script.CONFIG.repos) == ()
    assert script.held_orders() == ()
    closed = comms.goals.todos.get(todo.id)
    assert closed.state is DoneTodoState and closed.assignment is None
    assert closed.revision == todo.revision + 1 and not closed.pending
    assert isinstance(closed.order, MergedProblemOrder)
    assert closed.order.sha == sha and closed.order.original == original
    assert closed.order.id == original.id
    assert closed.strikes == todo.strikes
    assert FieldCodec.decode(type(closed), FieldCodec.encode(closed)) == closed

    def no_child(*args, **kwargs):
        pytest.fail('A repeated closed-order read launched a subprocess')

    with monkeypatch.context() as repeat:
        repeat.setattr(subprocess, 'run', no_child)
        assert script.COMMANDER.orders(script.CONFIG.repos) == ()
        assert comms.goals.todos.get(todo.id) == closed
    script.board()
    assert f'| {todo.id}:' not in (tmp_path / 'board.md').read_text()
    assert deadline_lines(script) == []
    assert comms.goals.todos.get(todo.id) == closed

    class TickComplete(Exception):
        pass

    def next_tick(seconds):
        raise TickComplete

    monkeypatch.setattr(script.time, 'sleep', next_tick)
    with pytest.raises(TickComplete):
        script.overdue()
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
        Repo(checkout=tmp_path / 'missing', github='test/repo').contains('a' * 40)
