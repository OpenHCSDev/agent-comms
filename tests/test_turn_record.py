"""Real registry/turn files and the public Comms phase entrypoint."""

import os

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.turn_phase import ModelWaitPhase, ToolRunningPhase


def test_phase_write_changes_only_the_leased_turn_and_refreshes_point_reads(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread(
        'worker', frozenset(), str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    owner = comms.agents.begin_turn('worker', 'original')
    lease = owner.turn_lease
    before = comms.registry.entry('worker')
    original_phase = before.entry.thread.turn_state.phase
    registry = tmp_path / 'registry.json'
    guard = tmp_path / '.registry-owner-guard'
    root_bytes = registry.read_bytes()
    root_stat = registry.stat()
    guard_bytes = guard.read_bytes() if guard.exists() else None

    phase = ToolRunningPhase(tools=(('read-one', 'read'),))
    effects = comms.agents.observe_native_phase(lease, phase)
    assert len(effects) == 1 and effects[0].phase == phase
    assert registry.read_bytes() == root_bytes
    assert registry.stat().st_mtime_ns == root_stat.st_mtime_ns
    assert (guard.read_bytes() if guard.exists() else None) == guard_bytes
    assert b'"phase"' not in registry.read_bytes()
    assert b'"active_turn"' not in registry.read_bytes()
    after = comms.registry.entry('worker')
    assert after.revision != before.revision
    assert after.entry.thread.turn_state.phase == phase
    assert before.entry.thread.turn_state.phase == original_phase
    assert comms.registry.snapshot().threads['worker'].turn_state.phase == phase

    current_record = after.entry.thread.turn_record.path
    current_bytes = current_record.read_bytes()
    assert comms.agents.observe_native_phase(lease, phase) == ()
    assert current_record.read_bytes() == current_bytes
    comms.agents.finish_turn(lease)
    comms.agents.begin_turn('worker', 'successor')
    assert comms.agents.observe_native_phase(lease, ModelWaitPhase()) == ()
    current = comms.registry.require('worker')
    assert current.active_turn.id == 'successor'
    assert current.turn_record.path != current_record
