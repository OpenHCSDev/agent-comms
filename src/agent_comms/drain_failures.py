"""What an owner records when draining its inbox fails."""

from __future__ import annotations

import asyncio
import sqlite3
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
from .native_pi import NativeRequestFailure

UNAVAILABLE_DRAIN_ERRORS = (OSError, sqlite3.Error, CoordinationError, RequestError, RelationViolationError)
DEFERRED_DRAIN_ERRORS = (NativeRequestFailure, *UNAVAILABLE_DRAIN_ERRORS)

if TYPE_CHECKING:
    from .comms import Comms
    from .thread_identity import OwnerIdentity


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

    async def record(kind, error) -> None:
        diagnostic = kind.from_error(owner, error)
        await Coordination.run_worker(partial(
            comms.agents.set_drain_diagnostic, thread, owner, diagnostic,
            source_error=error,
        ))

    try:
        yield
    except asyncio.CancelledError:
        raise
    except NativeRequestFailure as error:
        # This describes a native request failure, not an inbox read failure.
        # Settlement belongs to the request or selected participant that raised
        # it; recording a diagnostic neither settles an input nor permits replay.
        await record(NativeInputDrainDiagnostic, error)
        raise
    except UNAVAILABLE_DRAIN_ERRORS as error:
        await record(unavailable, error)
        raise
    except ValueError as error:
        await record(StoppedDrainDiagnostic, error)
        raise
    else:
        current = await Coordination.run_worker(partial(comms.registry.require, thread))
        if not current.turn_record.read().executing:
            await comms.agents.recover_drain(thread, owner, observed)
