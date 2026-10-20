"""Deadline strikes consume real Git/PR progress through the retained order store."""
import time
from contextlib import nullcontext
from dataclasses import replace

from agent_comms.channel_targets import CommandTarget
from agent_comms.commander.standing import UnownedWheelSpin
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.storage_rules import StorageRule
from agent_comms.todos import (
    MissedDeadlineStrike, NoCommitProgress, OpenPrProgress, OrderProgress,
    ProgressSource, PullRequests, PushedProgress, UnmergedPullRequest,
)
from test_order_progress import checkout, commit, git


def overdue(commander, checkout, monkeypatch):
    comms, _ = commander
    todo, unit = checkout
    now = time.time()
    monkeypatch.setattr(comms.registry, 'storage_rule', StorageRule(checkout_base=unit.parent))
    with comms.goals.todos.writing() as db:
        replace(todo, deadline=RecordedEventTime(now - 60), order=replace(todo.order,
            at=RecordedEventTime(now - 180),
            first_call_deadline=RecordedEventTime(now + 60))).upsert(db)
    return comms.goals.todos.get(todo.id)


def watch(comms):
    UnownedWheelSpin.watch_pass(lambda: nullcontext(comms))


def lines(comms):
    return [message.body for message in comms.views.history_page(
        CommandTarget.named('#comms'), viewer='lead').messages
        if message.body.startswith('Core strike')]


def test_pushed_past_due_order_gets_no_deadline_strike(commander, checkout, monkeypatch):
    comms, _ = commander
    todo = overdue(commander, checkout, monkeypatch)
    unit = checkout[1]
    commit(unit)
    git(unit, 'push', '-q', 'origin', 'HEAD:fix/p1')
    sha = git(unit, 'rev-parse', 'HEAD')
    source = ProgressSource.of(todo, comms.registry.storage_rule, PullRequests((todo,)))
    assert isinstance(OrderProgress.from_source(source), PushedProgress)
    watch(comms)
    assert comms.goals.todos.get(todo.id).strikes == () and lines(comms) == []
    # Control only the external GitHub response, never the derived progress or TodoStore.
    pr = UnmergedPullRequest(7, 'fix/p1', sha, updated_at=time.time(), orders=(todo.order.id,))
    monkeypatch.setattr(PullRequests, 'read',
                        lambda self, repo, state, order: (pr,) if state == 'open' else ())
    source = ProgressSource.of(todo, comms.registry.storage_rule, PullRequests((todo,)))
    assert isinstance(OrderProgress.from_source(source), OpenPrProgress)
    watch(comms)
    assert comms.goals.todos.get(todo.id).strikes == () and lines(comms) == []
    print('Past-due actual pushed branch and open PR at its SHA: no automatic strike.')


def test_stalled_past_due_order_gets_deadline_strike(commander, checkout, monkeypatch):
    comms, _ = commander
    todo = overdue(commander, checkout, monkeypatch)
    source = ProgressSource.of(todo, comms.registry.storage_rule, PullRequests((todo,)))
    assert isinstance(OrderProgress.from_source(source), NoCommitProgress)
    watch(comms)
    watch(comms)
    (strike,) = comms.goals.todos.get(todo.id).strikes
    assert isinstance(strike, MissedDeadlineStrike)
    assert len(lines(comms)) == 1 and 'minutes late' in lines(comms)[0]
    print(lines(comms)[0])
