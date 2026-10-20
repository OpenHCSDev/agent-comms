"""Authored retirement boundaries, with an isolated real root and no OS signal or launch.

The controlled stop member supplies process exit; actual registry/selection
validation and FencedOwnerBatch failure/resource dispatch remain production.
This is not an OFD, process retirement or installed central-batch qualification.
"""

from contextlib import contextmanager
import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.owner_cutover import PreserveOwnerRuntime
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.launch_runtime import RestartEnvironment
from agent_comms.owner_lifecycle import OwnerLifecycle
from agent_comms.owner_restart import FencedOwnerBatch, OwnerRestartRequest, StoppedOwnerFailure
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread


class AuthoredRetirement(OwnerLifecycle):
    def __init__(self, root, mode, monkeypatch):
        comms = Comms(root)
        super().__init__(comms.root, comms.registry, comms.bus)
        self.mode = mode
        self.originals = tuple(self.registry.new_thread(
            name, frozenset(), '/authored', created_at=float(index + 1),
            process_identity=ProcessIdentity(2147483647 - index, 1)) for index, name in enumerate(('first', 'second')))
        for thread in self.originals:
            self.registry.register(thread, StoppedThreadStatus())
        self.alive = {t.require_process(): True for t in self.originals}
        monkeypatch.setattr(ProcessIdentity, 'alive', lambda identity: self.alive[identity])
        self.stop_batches = []
        self.signal_boundaries = []
        self.wire_entries = 0
        self.active_wire_contexts = 0
        self.launches = []
        self.guard_refusal = None

    @contextmanager
    def restart_wire(self):
        # Context lifetime only: this sentinel is never presented as a real FD.
        self.active_wire_contexts += 1
        self.wire_entries += 1
        try:
            if self.mode == 'post_stop_witness' and self.wire_entries == 1:
                self.registry.register(self.originals[0], RunningThreadStatus())
            if self.mode == 'final_witness' and self.wire_entries == len(self.originals) + 1:
                self.registry.unregister('first')
            yield 91
        finally:
            self.active_wire_contexts -= 1

    def _stop_processes(self, owners):
        # Supply controlled physical outcomes at the current batch boundary.
        # Guard, registry retirement and stopped-witness checks remain real.
        self.stop_batches.append(tuple(thread.name for thread, _ in owners))
        for thread, generation in owners:
            if self.mode == 'second_guard' and thread.name == 'second':
                self.registry.unregister('second')
            try:
                with self._signal_guard(thread, generation):
                    self.signal_boundaries.append(thread.name)
                    self.alive[thread.require_process()] = False
            except RelationViolationError as error:
                self.guard_refusal = error
                raise

    def _launch_owner_unlocked(self, *args, **kwargs):
        self.launches.append((args, kwargs))
        raise AssertionError('A partial retirement must not launch any replacement')

    def batch(self):
        launches = tuple(RetainedOwnerLaunch(
            t.require_process(), '/authored/python', {'AGENT_COMMS_AGENT_BIN': '/authored/pi'},
        ) for t in self.originals)
        return FencedOwnerBatch(
            self, tuple((t, self.registry.snapshot().claim_generation(t.name))
                        for t in self.originals), launches,
            OwnerRestartRequest(), RestartEnvironment.inherit({}),
        )


class CapturedFailure(PreserveOwnerRuntime):
    def __init__(self):
        self.failure = None
        self.complete_calls = 0
        self.recover_calls = 0

    def complete(self, stopped):
        self.complete_calls += 1
        raise AssertionError('Partial retirement cannot enter installation')

    def recover(self, stopped):
        self.recover_calls += 1
        raise AssertionError('Partial retirement cannot authorize operation recovery')

    def failed(self, failure):
        self.failure = failure
        super().failed(failure)


@pytest.mark.parametrize('mode', ['second_guard', 'post_stop_witness', 'final_witness'])
def test_partial_retirement_transfers_original_input_and_refuses_launch(mode, monkeypatch, tmp_path):
    lifecycle = AuthoredRetirement(tmp_path, mode, monkeypatch)
    batch = lifecycle.batch()
    operation = CapturedFailure()
    with pytest.raises(StoppedOwnerFailure) as caught:
        batch.complete(operation)
    failure = caught.value
    assert failure is operation.failure
    assert failure.operation is operation
    assert isinstance(failure.__cause__, RelationViolationError)
    phase = failure.stopped
    assert isinstance(phase, FencedOwnerBatch)
    assert phase.captured == batch.captured and phase.launches == batch.launches
    assert phase.ownership is batch.ownership
    assert lifecycle.stop_batches == [('first', 'second')]
    # No incomplete batch grants stopped-owner authority, even after OS exit.
    assert phase.retired == ()
    assert [thread.name for thread, _ in phase.unconfirmed] == ['first', 'second']
    assert not lifecycle.alive[lifecycle.originals[0].require_process()]
    if mode == 'second_guard':
        assert failure.__cause__ is lifecycle.guard_refusal
        assert set(lifecycle.signal_boundaries) == {'first'}
        assert lifecycle.alive[lifecycle.originals[1].require_process()]
        assert lifecycle.wire_entries == 0
    elif mode == 'post_stop_witness':
        assert set(lifecycle.signal_boundaries) == {'first', 'second'}
        assert not any(lifecycle.alive.values())
        assert lifecycle.wire_entries == 1
    else:
        assert set(lifecycle.signal_boundaries) == {'first', 'second'}
        assert not any(lifecycle.alive.values())
        assert lifecycle.wire_entries == len(lifecycle.originals) + 1
    with pytest.raises(RelationViolationError, match='complete validated stopped handoff'):
        failure.recover()
    assert operation.complete_calls == operation.recover_calls == 0
    assert lifecycle.active_wire_contexts == 0
    assert lifecycle.launches == []
