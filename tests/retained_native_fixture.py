"""Enroll an already running native test host in the actual retained custody owner."""

import asyncio

from agent_comms.backend import PersistentPiSession
from agent_comms.native_attestation import ObservedAttestation
from agent_comms.native_custody import PiSessionChild
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.pi_payloads import StateData
from agent_comms.pi_rpc import PiRpcChannel


def retained_native_host(
    proc, launch, identity: NativeSessionIdentity, *, reader=None, stderr_task=None
):
    child = PiSessionChild(
        proc,
        reader if reader is not None else PiRpcChannel(proc.stdout),
        (
            stderr_task
            if stderr_task is not None
            else asyncio.create_task(PiSessionChild.stderr_tail(proc.stderr))
        ),
        (launch, (0, 0)),
        ObservedAttestation(
            StateData(session_id=identity.session_id, session_file=identity.session_file)
        ),
    )
    persistent = PersistentPiSession()
    assert persistent.retain(child, identity)
    return persistent
