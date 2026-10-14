"""What an owner records when draining its inbox fails."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from acp import RequestError

from .activity import (
    DrainReadiness,
    NativeInputDrainDiagnostic,
    ReadyDrainReadiness,
    StoppedDrainDiagnostic,
    UnavailableDrainDiagnostic,
)
from .coordination_errors import CoordinationError
from .coordinator import Coordination
from .native_pi import NativePiUnavailable

if TYPE_CHECKING:
    from .comms import Comms
    from .thread_identity import OwnerIdentity


@dataclass
class DrainFailureState:
    """One observer's recorded failure until its next successful drain."""

    readiness: DrainReadiness = field(default_factory=ReadyDrainReadiness)

    def recorded(self, diagnostic) -> None:
        self.readiness = diagnostic.readiness()

    async def succeeded(self, agents, thread: str) -> None:
        self.readiness = await self.readiness.retire(agents, thread)


@asynccontextmanager
async def drain_failures(
    comms: Comms,
    thread: str,
    owner: OwnerIdentity,
    retry: Callable[[], Awaitable[None]],
    state: DrainFailureState,
):
    """Record a recoverable failure and retire it after this observer recovers."""

    async def record(kind, error) -> None:
        diagnostic = kind(
            owner, type(error).__name__, str(error), recorded_at=time.time()
        )
        try:
            await Coordination.run_worker(partial(
                comms.agents.set_drain_diagnostic, thread, owner, diagnostic,
                source_error=error,
            ))
            state.recorded(diagnostic)
        finally:
            await retry()

    try:
        yield
    except asyncio.CancelledError:
        raise
    except NativePiUnavailable as error:
        # A failed native input has already been settled and published by its
        # request owner; it is never replayed, so the owner keeps taking later
        # inputs, as a restart would. This is not an inbox read failure and
        # must not borrow the Inbox unavailable presentation.
        await record(NativeInputDrainDiagnostic, error)
    except (OSError, ValueError, sqlite3.Error, CoordinationError, RequestError) as error:
        await record(UnavailableDrainDiagnostic, error)
    except Exception as error:
        diagnostic = StoppedDrainDiagnostic(
            owner, type(error).__name__, str(error), recorded_at=time.time()
        )
        await Coordination.run_worker(partial(
            comms.agents.set_drain_diagnostic, thread, owner, diagnostic,
            source_error=error,
        ))
        state.recorded(diagnostic)
        raise
    else:
        await state.succeeded(comms.agents, thread)
