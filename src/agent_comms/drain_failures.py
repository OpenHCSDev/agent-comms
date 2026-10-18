"""What an owner records when draining its inbox fails."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING

from acp import RequestError

from .activity import (
    NativeInputDrainDiagnostic,
    StoppedDrainDiagnostic,
    UnavailableDrainDiagnostic,
)
from .coordination_errors import CoordinationError
from .coordinator import Coordination
from .errors import RelationViolationError
from .native_pi import NativePiUnavailable

UNAVAILABLE_DRAIN_ERRORS = (OSError, sqlite3.Error, CoordinationError, RequestError, RelationViolationError)
DEFERRED_DRAIN_ERRORS = (NativePiUnavailable, *UNAVAILABLE_DRAIN_ERRORS)

if TYPE_CHECKING:
    from .comms import Comms
    from .thread_identity import OwnerIdentity


@asynccontextmanager
async def drain_failures(
    comms: Comms,
    thread: str,
    owner: OwnerIdentity,
):
    """Record and propagate failures; the observation owner waits for real change."""
    snapshot = await Coordination.run_worker(comms.registry.snapshot)
    observed = await Coordination.run_worker(partial(
        comms.agents.activity_of, thread, snapshot=snapshot,
    ))

    async def record(kind, error) -> None:
        diagnostic = kind(
            owner, type(error).__name__, str(error), recorded_at=time.time()
        )
        await Coordination.run_worker(partial(
            comms.agents.set_drain_diagnostic, thread, owner, diagnostic,
            source_error=error,
        ))

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
        raise
    except UNAVAILABLE_DRAIN_ERRORS as error:
        await record(UnavailableDrainDiagnostic, error)
        raise
    except ValueError as error:
        diagnostic = StoppedDrainDiagnostic(
            owner, type(error).__name__, str(error), recorded_at=time.time()
        )
        await Coordination.run_worker(partial(
            comms.agents.set_drain_diagnostic, thread, owner, diagnostic,
            source_error=error,
        ))
        raise
    else:
        current = await Coordination.run_worker(partial(comms.registry.require, thread))
        if not current.turn_record.read().executing:
            await comms.agents.recover_drain(thread, owner, observed)
