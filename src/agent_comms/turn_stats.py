"""Correlated statistics snapshots and their forwarding/settlement epochs."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import pi_commands as commands

if TYPE_CHECKING:
    from .backend import TurnSession


@dataclass
class StatsRequest:
    requested: bool = False
    state_id: str = ""
    usage_id: str = ""
    responses: set[str] = field(default_factory=set)
    complete: bool = False
    failed: bool = False
    busy: bool = False
    generation: int = 0
    settlement_count: int = 0

    async def request(self, session: TurnSession) -> None:
        if session.proc.stdin is None or self.requested:
            return
        self.requested = True
        self.generation = session.inputs.generation
        self.settlement_count = session.settlement_count
        self.state_id = f"agent-comms-stats-state-{secrets.token_hex(16)}"
        self.usage_id = f"agent-comms-stats-usage-{secrets.token_hex(16)}"
        try:
            state_request = commands.GetState(
                id=self.state_id if session.persistent_session else None
            )
            usage_request = commands.GetSessionStats(
                id=self.usage_id if session.persistent_session else None
            )
            session.proc.stdin.write(session.reader.encode(state_request))
            session.proc.stdin.write(session.reader.encode(usage_request))
            await session.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
