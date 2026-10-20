"""Real registry/turn files and the public Comms phase entrypoint."""

import json
import os

import pytest
from dataclasses import replace
from shutil import copytree

from agent_comms.errors import RelationViolationError
from agent_comms.registry_store import RegistryStore
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.turn_phase import ModelWaitPhase, ToolRunningPhase
from agent_comms.turn_lease import NoTurnRecord, StoredTurnRecord, StoredTurnReference


def test_executing_comes_from_the_record_declaration_without_reading(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread(
        'worker', frozenset(), str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    comms.agents.begin_turn('worker', 'running')
    owner = comms.registry.require('worker')

    def forbidden_read(record):
        raise AssertionError('execution status must not read a turn file')

    monkeypatch.setattr(StoredTurnRecord, 'read', forbidden_read)
    monkeypatch.setattr(NoTurnRecord, 'read', forbidden_read)
    monkeypatch.setattr(StoredTurnReference, 'read', forbidden_read)
    assert owner.turn_record.executing is True
    assert owner.executing is True
    assert NoTurnRecord().executing is False
    with pytest.raises(RelationViolationError, match='must be bound by its registry'):
        StoredTurnReference().executing


def test_phase_write_changes_only_the_leased_turn_at_point_of_use(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread(
        'worker', frozenset(), str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    owner = comms.agents.begin_turn('worker', 'original')
    lease = owner.turn_lease
    before = comms.registry.entry('worker')
    captured_state = before.entry.thread.turn_state
    registry = tmp_path / 'registry.json'
    guard = tmp_path / '.registry-owner-guard'
    root_bytes = registry.read_bytes()
    root_stat = registry.stat()
    guard_bytes = guard.read_bytes() if guard.exists() else None

    phase = ToolRunningPhase(tools=(('read-one', 'read'),))
    effects = comms.agents.observe_native_phase(lease, phase)
    assert len(effects) == 1 and effects[0].active.phase == phase
    assert registry.read_bytes() == root_bytes
    assert registry.stat().st_mtime_ns == root_stat.st_mtime_ns
    assert (guard.read_bytes() if guard.exists() else None) == guard_bytes
    assert b'"phase"' not in registry.read_bytes()
    assert b'"active_turn"' not in registry.read_bytes()
    after = comms.registry.entry('worker')
    assert after.revision == before.revision
    assert after.entry.thread.turn_state.active.phase == phase
    assert before.entry.thread.turn_state.active.phase == phase
    assert captured_state.active.phase != phase
    assert comms.registry.snapshot().threads['worker'].turn_state.active.phase == phase

    reads = []
    read = StoredTurnRecord.read

    def counted(record):
        reads.append(record.path)
        return read(record)

    monkeypatch.setattr(StoredTurnRecord, 'read', counted)
    snapshot = comms.registry.snapshot()
    comms.registry.entry('worker')
    replace(snapshot.threads['worker'], title='metadata only')
    assert reads == []

    current_record = after.entry.thread.turn_record.path
    current_bytes = current_record.read_bytes()
    assert comms.agents.observe_native_phase(lease, phase) == ()
    assert current_record.read_bytes() == current_bytes
    comms.agents.finish_turn(lease)
    comms.agents.begin_turn('worker', 'successor')
    assert comms.agents.observe_native_phase(lease, ModelWaitPhase()) == ()
    current = comms.registry.require('worker')
    assert current.turn_record.read().id == 'successor'
    assert current.turn_record.path != current_record


def test_copied_registry_resolves_turn_from_its_new_location(tmp_path):
    source = tmp_path / 'source'
    copied = tmp_path / 'copied'
    comms = wire(source)
    comms.registry.declare(comms.registry.new_thread(
        'worker', frozenset(), str(source),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    original = comms.agents.begin_turn('worker', 'preserved').turn_lease.record
    raw = json.loads((source / 'registry.json').read_text())
    reference = raw['threads']['worker']['turn_record']
    assert reference == {}
    copytree(source, copied)

    moved = RegistryStore(copied / 'registry.json')
    entry = moved.read_entry('worker').entry.thread
    assert entry.turn_record.read().id == 'preserved'
    assert entry.turn_record.path == copied / 'turns' / original.path.name
    assert moved.read().threads['worker'].turn_record.path == entry.turn_record.path
    assert moved.read().threads['worker'].turn_record.registry_path == copied / 'registry.json'

    forged = json.loads((copied / 'registry.json').read_text())
    forged['threads']['worker']['turn_record']['lookup'] = 'wrong-identity'
    with pytest.raises(RelationViolationError, match='Invalid registry document'):
        moved._decode(forged)


def test_unclaimed_record_still_publishes(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread(
        'worker', frozenset(), str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    record = comms.agents.begin_turn('worker', 'original').turn_lease.record
    published = []
    publish = StoredTurnRecord._publish_unlocked

    def observed(record, active):
        published.append(active.claimed_generation)
        publish(record, active)

    monkeypatch.setattr(StoredTurnRecord, '_publish_unlocked', observed)
    record.without_claim()
    record.without_claim()
    assert published == [None, None]
    assert record.read().claimed_generation is None
