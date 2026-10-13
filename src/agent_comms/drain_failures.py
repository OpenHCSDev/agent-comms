"""What an owner records when draining its inbox fails."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING

from acp import RequestError

from .activity import NativeInputDrainDiagnostic, StoppedDrainDiagnostic, UnavailableDrainDiagnostic
from .coordination_errors import CoordinationError
from .coordinator import Coordination
from .native_pi import NativePiUnavailable

if TYPE_CHECKING:
    from .comms import Comms
    from .thread_identity import OwnerIdentity


@asynccontextmanager
async def drain_failures(comms: Comms, thread: str, owner: OwnerIdentity):
    """Record the failure on the owner's thread; only an unexpected error ends the drain."""

    async def record(kind, error) -> None:
        await Coordination.run_worker(partial(
            comms.agents.set_drain_diagnostic, thread, owner,
            kind(owner, type(error).__name__, str(error)), source_error=error,
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
    except (OSError, ValueError, sqlite3.Error, CoordinationError, RequestError) as error:
        await record(UnavailableDrainDiagnostic, error)
    except Exception as error:
        await record(StoppedDrainDiagnostic, error)
        raise
