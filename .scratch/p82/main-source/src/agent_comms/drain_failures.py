"""What an owner records when draining its inbox fails."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager
from functools import partial
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from acp import RequestError

from .activity import (
    NativeInputDrainDiagnostic,
    StoppedDrainDiagnostic,
    UnavailableDrainDiagnostic,
)
from .coordination_errors import CoordinationError
from .coordinator import Coordination
from .native_pi import NativePiUnavailable

if TYPE_CHECKING:
    from .comms import Comms
    from .thread_identity import OwnerIdentity


@asynccontextmanager
async def drain_failures(
    comms: Comms,
    thread: str,
    owner: OwnerIdentity,
    retry: Callable[[], Awaitable[None]],
):
    """Record failures; a completed operation recovers its original observation."""
    observed = await Coordination.run_worker(partial(comms.agents.activity_of, thread))

    async def record(kind, error) -> None:
        diagnostic = kind(
            owner, type(error).__name__, str(error), recorded_at=time.time()
        )
        try:
            await Coordination.run_worker(partial(
                comms.agents.set_drain_diagnostic, thread, owner, diagnostic,
                source_error=error,
            ))
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
        raise
    else:
        await Coordination.run_worker(partial(
            comms.agents.recover_drain, thread, owner, observed,
        ))
