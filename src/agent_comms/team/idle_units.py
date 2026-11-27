"""Idle-executor selection shared by standing rules and bridge deferral hooks.

Kept separate from `agent_comms.team.standing` so importing `agent_comms.cli_bridges`
(for `HarnessSession` wire discovery on thin clients such as Android) never drags in
the installer package or `packaging`.
"""

from __future__ import annotations

import time
from typing import ClassVar

from agent_comms.comms import Comms
from agent_comms.execution.turn_bound import TurnsSince
from agent_comms.execution.turn_digests import TurnDigestRead
from agent_comms.planning.held_orders import HeldOrders
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.threads import Thread


class IdleExecutors:
    """Selection of active, recently-working threads that hold no board order."""

    recent: ClassVar[int] = 10 * 60

    @classmethod
    def read_idle(cls, comms: Comms) -> list[Thread]:
        """A fresh read and an already captured frame share the same idle selection."""
        return cls.idle(comms, comms.goals.held_orders())

    @classmethod
    def idle(
        cls,
        comms: Comms,
        scan: HeldOrders,
        *,
        snapshot: RegistrySnapshot | None = None,
    ) -> list[Thread]:
        """Available independent executors, regardless of who issued the request."""
        snapshot = snapshot if snapshot is not None else comms.registry.snapshot()
        held = {todo.holder for todo in scan.todos}
        bound = TurnsSince(time.time() - cls.recent)
        return sorted(
            (
                candidate
                for thread in snapshot.active_threads(snapshot.active_candidates())
                if thread.name not in held
                for candidate in thread.order_candidates()
                if TurnDigestRead(comms.root, candidate, bound).read().turns
            ),
            key=lambda thread: thread.name,
        )
