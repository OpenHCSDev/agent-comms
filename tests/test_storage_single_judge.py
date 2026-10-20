"""Real Git cleanup is admitted once; native results carry only actual preflight denials."""
import collections
import json
import shlex
import subprocess
from dataclasses import replace

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.native_tool_call import CodingCall
from agent_comms.pi_payloads import StorageDenialToolDetails
from agent_comms.storage_rules import StorageDenied, StorageRule, UnitStorage
from test_commander_problem_orders import create


def git(seed, *args):
    return subprocess.run(('git', '-C', str(seed), *map(str, args)), check=True,
                          capture_output=True, text=True)


@pytest.fixture
def checkouts(commander, tmp_path):
    comms, _ = commander
    seed = tmp_path / 'repository'
    seed.mkdir()
    git(seed, 'init', '-q')
    git(seed, '-c', 'user.name=P303', '-c', 'user.email=p303@example.invalid',
        'commit', '-q', '--allow-empty', '-m', 'fixture')
    base = tmp_path / 'checkouts'
    home = base / 'worker'
    home.mkdir(parents=True)
    extras = tuple(base / f'worker--P{n}' for n in (1, 2, 3))
    for path in extras[:2]:
        git(seed, 'worktree', 'add', '-q', str(path))
    rule = StorageRule(checkout_base=base, scratch_base=tmp_path / 'scratch',
                       volume=tmp_path, floor_gib=0, max_checkouts=2)
    (comms.root / 'storage.json').write_text(json.dumps(FieldCodec.encode(rule)))
    comms.registry.storage_rule = rule
    comms.registry.declare(replace(comms.registry.require('worker'), worktree=str(extras[0])))
    yield comms, seed, home, extras
    for path in extras:
        if path.exists():
            git(seed, 'worktree', 'remove', '--force', str(path))


def command(seed, action, path):
    return shlex.join(('git', '-C', str(seed), 'worktree', action, str(path)))


def native_line(identity, message):
    return (json.dumps({'type': 'message', 'id': identity,
                        'timestamp': '2026-10-19T22:50:00Z', 'message': message}) + '\n').encode()


def test_removal_that_ran_is_never_struck_afterwards(checkouts, commander, commander_script,
                                                    tool_watch, monkeypatch):
    comms, seed, home, (first, second, third) = checkouts
    before = create(commander, 'P1')
    text = command(seed, 'remove', first)
    call = CodingCall.decode('removed', 'bash', {'command': text})
    call.decide_storage(comms.registry, 'worker')
    result = subprocess.run(text, shell=True, check=True, capture_output=True, text=True)
    assert not first.exists()

    def forbidden(*args):
        raise AssertionError('post-call observer re-read storage')

    monkeypatch.setattr(StorageRule, 'checkouts_of', forbidden)
    turns = collections.defaultdict(list)
    tool_watch.follow(commander_script[0], turns, 'worker', native_line('call-removed', {
        'role': 'assistant', 'stopReason': 'toolUse', 'content': [
            {'type': 'toolCall', 'id': call.call_id, 'name': 'bash', 'arguments': call.arguments}]}))
    tool_watch.follow(commander_script[0], turns, 'worker', native_line('result-removed', {
        'role': 'toolResult', 'toolCallId': call.call_id, 'toolName': 'bash', 'isError': False,
        'content': [{'type': 'text', 'text': result.stdout}]}))
    # A command's error text is not an owner-recorded preflight refusal either.
    tool_watch.follow(commander_script[0], turns, 'worker', native_line('ordinary-error', {
        'role': 'toolResult', 'toolCallId': 'ordinary-error', 'toolName': 'bash', 'isError': True,
        'content': [{'type': 'text', 'text': 'StorageRule: text printed by a failed command'}]}))
    assert comms.goals.todos.get('P1') == before


def test_remove_remove_add_projects_in_order_but_third_extra_is_refused(checkouts):
    comms, seed, home, (first, second, third) = checkouts
    allowed = ' && '.join((command(seed, 'remove', first), command(seed, 'remove', second),
                           command(seed, 'add', third)))
    assert len(CodingCall.decode('cleanup', 'bash', {'command': allowed}).decide_storage(
        comms.registry, 'worker')) == 3
    denied = ' && '.join(command(seed, 'add', home.parent / f'worker--P{n}') for n in (3, 4, 5))
    with pytest.raises(StorageDenied, match='at most 2'):
        CodingCall.decode('over-quota', 'bash', {'command': denied}).decide_storage(
            comms.registry, 'worker')
    assert first.exists() and second.exists() and not third.exists()
    subprocess.run(allowed, shell=True, check=True, capture_output=True, text=True)
    thread = comms.registry.require('worker')
    assert comms.registry.storage_rule.checkouts_of(thread) == (home, third)


def test_missing_declared_home_does_not_protect_current_extra_from_removal(checkouts):
    comms, seed, home, (first, second, third) = checkouts
    home.rmdir()
    thread = comms.registry.require('worker')
    assert thread.worktree == str(first)
    storage = UnitStorage.read(comms.registry.storage_rule, thread)
    assert storage.checkouts == (home, first, second)
    assert 'declared home' in storage.line() and 'is missing' in storage.line()
    text = command(seed, 'remove', first)
    CodingCall.decode('missing-home-cleanup', 'bash', {'command': text}).decide_storage(
        comms.registry, 'worker')
    subprocess.run(text, shell=True, check=True, capture_output=True, text=True)
    assert not first.exists() and not home.exists()
