from __future__ import annotations

import asyncio

from agent_comms.execution.child_process import ProcessState
from agent_comms.execution.native_child import (
    BorrowedNative,
    EmptyNative,
    NativeChildState,
    PiSessionChild,
    RetainedNative,
)
from agent_comms.execution.native_session_reopen import NativeSessionIdentity
from agent_comms.execution.session_retention import NativeSessionRetention
from agent_comms.execution.session_revision import SessionRevision, SessionRevisionUnavailable

class PersistentPiSession(
    NativeSessionRetention[PiSessionChild, NativeChildState, NativeSessionIdentity],
):
    """Serialize borrowing and retirement of one native session's actual child."""

    def __init__(self) -> None:

        self.lock = asyncio.Lock()
        self.child_state: NativeChildState = EmptyNative()

    @property
    def available(self) -> bool:
        return self.child_state.available

    @property
    def runtime_process(self) -> ProcessState:
        return self.child_state.process_identity

    async def open(self, launch, *, reuse, startup, watchdog) -> PiSessionChild:

        key = (launch, launch.configuration.auth_revision())
        child = self.child_state.reuse(key) if reuse else None
        if child is None:
            await self.close()
            attestation = launch.session.attestation()
            watchdog.launching(asyncio.get_running_loop().time, launch.session.path)
            with startup.measurements.operation("native_spawn"):
                child = await PiSessionChild.start(key, attestation)
            self.child_state = BorrowedNative(child, self.child_state)
            watchdog.spawned()
        else:
            watchdog.launching(asyncio.get_running_loop().time, launch.session.path)
            self.child_state = BorrowedNative(child, EmptyNative())
            watchdog.reused()
        return child

    async def context_for_input(self, thread, context):
        async with self.lock:
            return await self.child_state.context_for_input(thread, context)

    def retain(self, child: PiSessionChild, identity: NativeSessionIdentity) -> bool:

        if not child.proc.alive():
            return False
        try:
            launch = child.key[0]
            revision = SessionRevision.observe(launch.session.files(launch.root)).require_available()
        except SessionRevisionUnavailable:
            return False
        self.child_state = RetainedNative(child, identity, revision)
        return True

    async def close(self) -> None:
        await self._finish_retirement(self.child_state.retire())

    async def _finish_retirement(self, retiring: NativeChildState) -> None:
        self.child_state = retiring
        try:
            await retiring.closed()
        finally:
            self.child_state = retiring.settled

    async def close_idle(self) -> None:
        async with self.lock:
            await self.close()
