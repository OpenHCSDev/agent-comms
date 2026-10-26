from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
import asyncio

if TYPE_CHECKING:
    from agent_comms.comms import Comms


class HeldBusWrite:
    """A CLI process's publication waits out maintenance on its own loop, never retrying a
    write; a service's worker thread refuses at the store's write guard instead."""

    def write_when_open[Result](self, comms: Comms, operation: Callable[[], Result]) -> Result:
        return asyncio.run(comms.bus.when_open(operation))
