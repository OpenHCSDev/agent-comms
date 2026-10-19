"""Failures use the original turn record; fresh work does not need a restart."""
import os
import shutil
import time

from agent_comms.channels import AllChannel
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.thread_status import RunningThreadStatus
from agent_comms.turn_lease import FailedTurn, TurnEnding
from agent_comms.wire_record import WireRecord
import json


def copied_owner(tmp_path, *, parent='commander'):
    original = Comms(tmp_path / 'original')
    original.registry.declare(original.registry.new_thread('commander', frozenset(), str(tmp_path)))
    owner = original.registry.new_thread('unit', frozenset(), str(tmp_path), parent=parent,
        process_identity=ProcessIdentity.capture(os.getpid()))
    original.registry.declare(owner, RunningThreadStatus())
    shutil.copytree(original.root, tmp_path / 'copy')
    return Comms(tmp_path / 'copy')


def messages(comms):
    return [message for line in comms.bus.log.path.read_text().splitlines()
            for message in WireRecord.public_from_wire(json.loads(line)).messages()]


def test_registry_read_failure_ends_loudly_and_new_input_is_independent(tmp_path, monkeypatch):
    comms = copied_owner(tmp_path)
    owner = comms.agents.begin_turn('unit', 'failed-one', 'first task')
    lease = owner.turn_lease
    before = lease.record.read()
    read = comms.registry.require
    def fail_once(name):
        monkeypatch.setattr(comms.registry, 'require', read)
        raise ValueError('P689 injected registry read failure')
    monkeypatch.setattr(comms.registry, 'require', fail_once)
    started = time.monotonic()
    try:
        comms.registry.require('unit')
    except ValueError as error:
        comms.agents.finish_turn(lease, TurnEnding.from_exception(error))
    assert time.monotonic() - started < 1
    ended = lease.record.read()
    assert isinstance(ended, FailedTurn)
    assert ended.turn == before
    assert 'P689 injected registry read failure' in ended.error
    assert not comms.registry.require('unit').executing
    notices = messages(comms)
    assert len(notices) == 1 and notices[0].target == 'commander'
    assert ended.error in notices[0].body
    # Closing the same exact original record cannot send the failure twice.
    comms.agents.finish_turn(lease)
    assert messages(comms) == notices
    fresh = comms.messaging.send('commander', 'unit', 'Fresh independent DM').message
    next_owner = comms.agents.begin_turn('unit', 'fresh-two', fresh.body)
    assert next_owner.turn_lease.identity.generation > lease.identity.generation
    comms.agents.finish_turn(next_owner.turn_lease)
    assert lease.record.read() == ended
    print('P689: registry read failed; failed record and commander alert; fresh DM accepted without stop')


def test_parentless_failure_is_posted_to_channel(tmp_path):
    comms = copied_owner(tmp_path, parent=None)
    owner = comms.agents.begin_turn('unit', 'parentless-one', 'first task')
    lease = owner.turn_lease
    comms.agents.finish_turn(lease, TurnEnding.from_exception(RuntimeError('parentless fault')))
    notice, = messages(comms)
    assert notice.target == AllChannel.declared_name and 'parentless fault' in notice.body
    assert not comms.registry.require('unit').executing
