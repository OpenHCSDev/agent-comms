"""Stop fences the real native GRANT without changing coordinator generations."""

import asyncio
import multiprocessing
import os
import signal
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path

import pytest

from agent_comms.attempt_recovery import RecoveryMonitorCapability
from agent_comms.attempt_states import AttemptFailedAttempt, UnknownOutcomeAttempt
from agent_comms.comms import wire
from agent_comms.coordination_errors import RecoveryBlocked, StaleFence
from agent_comms.coordinator import Coordination
from agent_comms.native_runtime_input import NativeRuntimeInput
from test_native_failure_recovery import ReleasedNativeInput, prepared_native_input


def receive(connection):
    if not connection.poll(30):
        raise TimeoutError("native owner did not answer")
    return connection.recv()


async def grant_owner(directory, connection):
    async with prepared_native_input(Path(directory), max_attempts=2) as claim:
        participant = claim.selected
        comms = participant.comms

        def stopped(signum, frame):
            connection.send(comms.registry.snapshot().claim_generation(participant.owner.thread.name))
            os._exit(0)

        signal.signal(signal.SIGTERM, stopped)
        connection.send(ReleasedNativeInput(
            comms.root, participant.root_id, participant.batch.execution_id,
            claim.input_id, claim.session.identity.path,
        ))
        connection.recv()

        def record_grant():
            with claim(claim.session.identity):
                pass

        try:
            await Coordination.run_worker(record_grant)
        except StaleFence as error:
            connection.send(error)
        else:
            connection.send(None)
        connection.recv()


def owner_process(directory, connection):
    with connection:
        asyncio.run(grant_owner(directory, connection))


@dataclass(frozen=True)
class LiveInput:
    process: BaseProcess
    connection: Connection
    source: ReleasedNativeInput


@pytest.fixture
def live_input(private_directory):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=owner_process, args=(str(private_directory), child))
    process.start()
    child.close()
    try:
        yield LiveInput(process, parent, receive(parent))
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)
        assert not process.is_alive()
        process.close()
        parent.close()


@pytest.mark.parametrize(
    ("stop_first", "outcome"),
    ((True, AttemptFailedAttempt), (False, UnknownOutcomeAttempt)),
    ids=("stop-first", "grant-first"),
)
def test_registry_stop_orders_real_native_grant_and_preserves_input(live_input, stop_first, outcome):
    source = live_input.source
    comms = wire(source.root)
    owner = comms.registry.require("beta")
    before = comms.registry.snapshot().claim_generation(owner.name)
    owners = ((owner, before),)
    with Coordination(source.root / "coordination.sqlite3") as store:
        coordinator_before = store.participants.get(owner.incarnation.lookup).owner_identity
        attempt_before = store.get(source.execution_id).attempt

    if stop_first:
        comms.owners._finish_stopped_owners(owners)
    live_input.connection.send(None)
    result = receive(live_input.connection)
    if stop_first:
        assert type(result) is StaleFence
    else:
        assert result is None
        comms.owners._finish_stopped_owners(owners)

    after = comms.registry.snapshot().claim_generation(owner.name)
    assert after > before
    release = comms.owners.releases.read_many((owner.name,))[owner.name]
    assert release.current(comms.registry.snapshot(), owner, before)
    with Coordination(source.root / "coordination.sqlite3") as store:
        original = NativeRuntimeInput.require(store.session._connection, input_id=source.input_id)
        assert original.sent_owner_claimed_generation.matches(before) is (not stop_first)
        assert store.get(source.execution_id).attempt == attempt_before
        assert store.participants.get(owner.incarnation.lookup).owner_identity == coordinator_before
        original.sent_owner_claimed_generation.require_release(release, comms.registry.snapshot(), owner)

    # The real signal guard must accept the original release after the claim moved.
    comms.owners._stop_processes(owners)
    assert receive(live_input.connection) == after
    live_input.process.join(5)
    assert not live_input.process.is_alive()
    comms.owners._finish_stopped_owners(owners)
    assert comms.registry.snapshot().claim_generation(owner.name) == after
    RecoveryMonitorCapability.settle_stopped_owners(comms.owners, (owner,))
    with Coordination(source.root / "coordination.sqlite3") as store:
        settled = store.get(source.execution_id)
        assert not settled.is_current
        assert type(settled.attempt.lifecycle) is outcome
        assert NativeRuntimeInput.require(store.session._connection, input_id=source.input_id) == original
        assert settled.attempt.attempt_ordinal < settled.execution.max_attempts
        if stop_first:
            settled.attempt.require_retry(settled)
        else:
            with pytest.raises(RecoveryBlocked):
                settled.attempt.require_retry(settled)


def test_public_stop_moves_claim_before_the_real_signal(live_input):
    comms = wire(live_input.source.root)
    owner = comms.registry.require("beta")
    before = comms.registry.snapshot().claim_generation(owner.name)
    # Keep the original process at its pre-GRANT boundary; Stop owns every phase.
    comms.owners.stop(owner.name)
    observed_at_signal = receive(live_input.connection)
    assert observed_at_signal > before
    assert observed_at_signal == comms.registry.snapshot().claim_generation(owner.name)
    live_input.process.join(5)
    assert not live_input.process.is_alive()
