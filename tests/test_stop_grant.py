"""Stop fences the real native GRANT before retiring its logical session."""
from __future__ import annotations

from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path

import pytest

from agent_comms.execution.acp import CommsAgent
from agent_comms.coordination.attempt_recovery import RecoveryMonitorCapability
from agent_comms.coordination.attempt_states import UnknownOutcomeAttempt
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.errors import RecoveryBlocked, StaleFence
from agent_comms.execution.native_runtime_input import NativeRuntimeInput
from agent_comms.installer.native_package import VerifiedNativePackage
from agent_comms.nominal.field_codec import AcpSessionId
from agent_comms.private_send_claim import PrivateSendClaim
from test_native_failure_recovery import prepared_native_input


@pytest.fixture
async def live_input(
    private_directory: Path, native_runtime_package: VerifiedNativePackage,
) -> AsyncIterator[PrivateSendClaim]:
    async with prepared_native_input(private_directory, max_attempts=2) as claim:
        comms = claim.selected.comms
        agent = CommsAgent(comms, auto_wake=True)
        try:
            # Bind the actual session/socket, but do not start its input drain:
            # the control owns the unsent/GRANT boundary, never a model prompt.
            await agent.sessions.bind_owned(
                comms.registry.require("beta"), AcpSessionId("beta"),
            )
            yield claim
        finally:
            await agent.shutdown()


@pytest.mark.parametrize("stop_first", (True, False), ids=("stop-first", "grant-first"))
async def test_registry_stop_orders_real_native_grant_and_preserves_input(
    live_input: PrivateSendClaim, stop_first: bool,
) -> None:
    claim = live_input
    participant = claim.selected
    comms = participant.comms
    owner = comms.registry.require("beta")
    before = comms.registry.snapshot().claim_generation(owner.name)
    with Coordination(comms.root / "coordination.sqlite3") as store:
        coordinator_before = store.participants.get(owner.incarnation.lookup).owner_identity
        attempt_before = store.get(participant.batch.execution_id).attempt

    def record_grant() -> None:
        with claim(claim.session.identity):
            pass

    if not stop_first:
        await Coordination.run_worker(record_grant)
    owners = await Coordination.run_worker(partial(comms.owners.fence_stop, owner.name))
    ((_, fenced),) = owners
    assert fenced > before
    if stop_first:
        with pytest.raises(StaleFence):
            await Coordination.run_worker(record_grant)

    with Coordination(comms.root / "coordination.sqlite3") as store:
        original = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
        assert original.sent_owner_claimed_generation.matches(before) is (not stop_first)
        assert store.get(participant.batch.execution_id).attempt == attempt_before
        assert store.participants.get(owner.incarnation.lookup).owner_identity == coordinator_before

    # The real receiver requires a fenced owner before it can retire the session.
    await Coordination.run_worker(partial(comms.owners._stop_processes, owners))
    snapshot = comms.registry.snapshot()
    after = snapshot.claim_generation(owner.name)
    assert after > fenced
    release = comms.owners.releases.read_many((owner.name,))[owner.name]
    assert release.current(snapshot, owner, fenced)
    original.sent_owner_claimed_generation.require_release(release, snapshot, owner)
    assert owner.require_process().alive()  # Logical retirement must not kill its shared host.
    comms.owners._finish_stopped_owners(owners)
    assert comms.registry.snapshot().claim_generation(owner.name) == after

    RecoveryMonitorCapability.settle_stopped_owners(comms.owners, (owner,))
    with Coordination(comms.root / "coordination.sqlite3") as store:
        settled = store.get(participant.batch.execution_id)
        assert not settled.is_current
        # A missing context proof cannot establish unsent input in either order.
        assert type(settled.attempt.lifecycle) is UnknownOutcomeAttempt
        assert NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id) == original
        assert settled.attempt.attempt_ordinal < settled.execution.max_attempts
        with pytest.raises(RecoveryBlocked):
            settled.attempt.require_retry(settled)


async def test_public_stop_fences_grant_before_session_retirement(
    live_input: PrivateSendClaim,
) -> None:
    claim = live_input
    comms = claim.selected.comms
    owner = comms.registry.require("beta")
    before = comms.registry.snapshot().claim_generation(owner.name)
    with Coordination(comms.root / "coordination.sqlite3") as store:
        original = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)

    # Keep the original input at its pre-GRANT boundary; Stop owns every phase.
    await Coordination.run_worker(partial(comms.owners.stop, owner.name))
    snapshot = comms.registry.snapshot()
    release = comms.owners.releases.read_many((owner.name,))[owner.name]
    assert release.before > before
    assert release.current(snapshot, owner, release.before)
    assert owner.require_process().alive()
    with Coordination(comms.root / "coordination.sqlite3") as store:
        settled = store.get(claim.selected.batch.execution_id)
        assert not settled.is_current
        assert type(settled.attempt.lifecycle) is UnknownOutcomeAttempt
        assert NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id) == original
        with pytest.raises(RecoveryBlocked):
            settled.attempt.require_retry(settled)
