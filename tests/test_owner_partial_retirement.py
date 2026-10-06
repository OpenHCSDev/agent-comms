"""Authored in-memory retirement boundaries, with no OS signal, root or launch.

The controlled stop member supplies process exit; actual registry/selection
validation and FencedOwnerBatch failure/resource dispatch remain production.
This is not an OFD, process retirement or installed central-batch qualification.
"""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.owner_cutover import PreserveOwnerRuntime
from agent_comms.owner_launch import RestartEnvironment, RetainedOwnerLaunch
from agent_comms.owner_lifecycle import OwnerLifecycle
from agent_comms.owner_restart import FencedOwnerBatch, OwnerRestartRequest, StoppedOwnerFailure
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread


class AuthoredRetirement(OwnerLifecycle):
    def __init__(self, mode, monkeypatch):
        self.root = Path('/authored')
        self.mode = mode
        self.originals = tuple(Thread(
            name, frozenset(), '/authored', created_at=float(index + 1),
            process_identity=ProcessIdentity(2147483647 - index, 1),
        ) for index, name in enumerate(('first', 'second')))
        self.snapshot = RegistrySnapshot(
            threads={t.name: t for t in self.originals},
            statuses={t.name: StoppedThreadStatus() for t in self.originals},
            last_seen={t.name: 1.0 for t in self.originals}, aliases={},
            owner_generations={t.name: 4 for t in self.originals},
            admission_generations={t.name: 5 for t in self.originals},
        )
        self.registry = SimpleNamespace(snapshot=lambda: self.snapshot)
        self.releases = SimpleNamespace(read=lambda: {})
        self.alive = {t.require_process(): True for t in self.originals}
        monkeypatch.setattr(ProcessIdentity, 'alive', lambda identity: self.alive[identity])
        self.stop_attempts = []
        self.signal_boundaries = []
        self.active_wire_contexts = 0
        self.launches = []
        self.guard_refusal = None

    @contextmanager
    def restart_wire(self):
        # Context lifetime only: this sentinel is never presented as a real FD.
        self.active_wire_contexts += 1
        try:
            yield 91
        finally:
            self.active_wire_contexts -= 1

    def _stop_process(self, thread, generation):
        self.stop_attempts.append(thread.name)
        if self.mode == 'second_guard' and thread.name == 'second':
            self.snapshot = replace(self.snapshot, admission_generations={
                **self.snapshot.admission_generations, 'second': 6,
            })
        try:
            self._require_same_stop_owner(thread, generation)
        except BaseException as error:
            self.guard_refusal = error
            raise
        self.signal_boundaries.append(thread.name)
        self.alive[thread.require_process()] = False
        if self.mode == 'post_stop_witness':
            self.snapshot = replace(self.snapshot, statuses={
                **self.snapshot.statuses, thread.name: RunningThreadStatus(),
            })
        if self.mode == 'final_witness' and thread.name == 'second':
            self.snapshot = replace(self.snapshot, admission_generations={
                **self.snapshot.admission_generations, 'first': 6,
            })

    def _launch_owner_unlocked(self, *args, **kwargs):
        self.launches.append((args, kwargs))
        raise AssertionError('A partial retirement must not launch any replacement')

    def batch(self):
        launches = tuple(RetainedOwnerLaunch(
            t.require_process(), '/authored/python', {'AGENT_COMMS_AGENT_BIN': '/authored/pi'},
        ) for t in self.originals)
        return FencedOwnerBatch(
            self, tuple((t, 5) for t in self.originals), launches,
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
def test_partial_retirement_transfers_original_custody_and_refuses_launch(mode, monkeypatch):
    lifecycle = AuthoredRetirement(mode, monkeypatch)
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
    assert phase.custody is batch.custody
    if mode == 'second_guard':
        assert failure.__cause__ is lifecycle.guard_refusal
        assert lifecycle.signal_boundaries == ['first']
        assert [x.name for x in phase.retired] == ['first']
        assert [x[0].name for x in phase.unconfirmed] == ['second']
        assert phase.exited_processes == (batch.launches[0],)
        assert lifecycle.alive[lifecycle.originals[1].require_process()]
    elif mode == 'post_stop_witness':
        assert lifecycle.stop_attempts == ['first']
        assert lifecycle.signal_boundaries == ['first']
        assert phase.exited_processes == (batch.launches[0],)
        assert phase.retired == ()
        assert [x[0].name for x in phase.unconfirmed] == ['first', 'second']
        assert not lifecycle.alive[lifecycle.originals[0].require_process()]
        assert lifecycle.alive[lifecycle.originals[1].require_process()]
    else:
        assert lifecycle.signal_boundaries == ['first', 'second']
        assert phase.exited_processes == batch.launches
        assert phase.retired == ()
        assert [x[0].name for x in phase.unconfirmed] == ['first', 'second']
        assert not any(lifecycle.alive.values())
        assert lifecycle.active_wire_contexts == 1
    with pytest.raises(RelationViolationError, match='complete validated stopped handoff'):
        failure.recover()
    assert operation.complete_calls == operation.recover_calls == 0
    assert lifecycle.launches == []
    failure.abandon()
    assert lifecycle.active_wire_contexts == 0
    assert lifecycle.launches == []
