"""What an owner records when draining its inbox fails."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING


from acp.exceptions import RequestError

from .activity import (
    DrainDiagnostic,
    NativeInputDrainDiagnostic,
    StoppedDrainDiagnostic,
    UnavailableDrainDiagnostic,
)
from .coordination_errors import DrainDeferred, DrainUnavailable
from .coordinator import Coordination
from .native_failures import NativeRequestFailure

# Failures raised outside our hierarchy, which cannot inherit the drain capabilities.
EXTERNAL_UNAVAILABLE_ERRORS = (OSError, sqlite3.Error, RequestError)
UNAVAILABLE_DRAIN_ERRORS = (DrainUnavailable, *EXTERNAL_UNAVAILABLE_ERRORS)
DEFERRED_DRAIN_ERRORS = (DrainDeferred, *EXTERNAL_UNAVAILABLE_ERRORS)

if TYPE_CHECKING:
    from .comms import Comms
    from .thread_identity import OwnerIdentity


async def record_drain_failure(
    comms: Comms, thread: str, owner: OwnerIdentity,
    kind: type[DrainDiagnostic], error: Exception,
) -> None:
    diagnostic = kind.from_error(owner, error)
    await Coordination.run_worker(partial(
        comms.agents.set_drain_diagnostic, thread, owner, diagnostic,
        source_error=error,
    ))


@asynccontextmanager
async def drain_failures(
    comms: Comms,
    thread: str,
    owner: OwnerIdentity,
    unavailable: type[UnavailableDrainDiagnostic] = UnavailableDrainDiagnostic,
):
    """Record and propagate failures; the observation owner waits for real change."""
    snapshot = await Coordination.run_worker(comms.registry.snapshot)
    observed = await Coordination.run_worker(partial(
        comms.agents.activity_of, thread, snapshot=snapshot,
    ))

    try:
        yield
    except asyncio.CancelledError:
        raise
    except NativeRequestFailure as error:
        # This describes a native request failure, not an inbox read failure.
        # Settlement belongs to the request or selected participant that raised
        # it; recording a diagnostic neither settles an input nor permits replay.
        await record_drain_failure(comms, thread, owner, NativeInputDrainDiagnostic, error)
        raise
    except UNAVAILABLE_DRAIN_ERRORS as error:
        await record_drain_failure(comms, thread, owner, unavailable, error)
        raise
    except ValueError as error:
        await record_drain_failure(comms, thread, owner, StoppedDrainDiagnostic, error)
        raise
    else:
        await comms.agents.recover_drain(thread, owner, observed)
