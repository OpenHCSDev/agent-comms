"""Merge observation closes the real stored order, without a completion request."""
from agent_comms.repository_name import RepositoryName
from dataclasses import replace
import subprocess

import pytest

from agent_comms.repo import Repo
from agent_comms.todos import (DoneTodoState, MergedProblemOrder, MergedPullRequest, PullRequests, UnorderedProblemOrder)
from test_order_progress import checkout, commit, git  # noqa: F401  (checkout is a fixture)


def test_order_reads_stay_unchanged_during_install_until_reconciliation(
        commander, commander_script, checkout, tmp_path, monkeypatch, capsys):
    import sys
    from agent_comms.commander.cli import main
    from agent_comms.maintenance_barrier import (
        InstallingPhase, MaintenanceBarrier, MaintenanceOperator, PausedPhase, ReadyPhase,
    )
    from agent_comms.todos import TodoStore

    comms, _ = commander
    script, _ = commander_script
    todo, unit = checkout
    commit(unit)
    git(unit, 'switch', '-q', 'main')
    git(unit, 'merge', '-q', '--ff-only', 'fix/p1')
    sha = git(unit, 'rev-parse', 'HEAD')
    todo = replace(todo, order=replace(todo.order,
        check=replace(todo.order.check, directory=str(unit))))
    with comms.goals.todos.writing() as db:
        todo.upsert(db)
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
        barrier.advance(hold, ReadyPhase)

    lookups.clear()
    comms.goals.todos.reconcile_merges(
        comms.registry.require('lead').incarnation, comms.registry.storage_rule)
    closed = comms.goals.todos.get(todo.id)
    assert lookups == ['merged'], 'Merge reconciliation did not run exactly once'
    assert closed.state is DoneTodoState and closed.assignment is None
    assert closed.revision == todo.revision + 1
    assert isinstance(closed.order, MergedProblemOrder)
    assert closed.order.sha == sha
    assert script.todos() == ()
    assert capsys.readouterr().out == ''


def test_new_revision_is_not_closed_from_an_old_merge_observation(
        commander, commander_script, checkout, monkeypatch):
    comms, _ = commander
    script, installed = commander_script
    todo, _ = checkout
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
                                      comms.registry.storage_rule)
    assert comms.goals.todos.get(todo.id) == newer


def test_only_checked_orders_are_selected_for_merge_reconciliation(commander, checkout):
    comms, _ = commander
    todo, _ = checkout
    unordered = replace(todo, order=UnorderedProblemOrder())
    with comms.goals.todos.writing() as db:
        unordered.upsert(db)
    comms.goals.todos.reconcile_merges(comms.registry.require('lead').incarnation,
                                      comms.registry.storage_rule)
    assert comms.goals.todos.get(todo.id) == unordered


def test_failed_git_is_not_a_negative_ancestry_result(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        Repo(checkout=tmp_path / 'missing', github=RepositoryName.from_wire('test/repo')).contains('a' * 40)


def test_a_merge_made_after_the_last_fetch_is_fetched_then_answered(tmp_path):
    origin, installed = tmp_path / 'origin', tmp_path / 'installed'
    origin.mkdir()
    git(origin, 'init', '-q', '-b', 'main')
    git(origin, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'base')
    git(tmp_path, 'clone', '-q', str(origin), str(installed))
    git(origin, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'merged later')
    sha = git(origin, 'rev-parse', 'HEAD')
    repo = Repo(checkout=installed, github=RepositoryName.from_wire('test/repo'))

    assert not repo.contains(sha)
    assert repo.contains(sha, 'origin/main')
    assert not repo.contains('b' * 40)
