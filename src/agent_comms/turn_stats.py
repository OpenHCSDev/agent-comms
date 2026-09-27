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
            state_request = {"type": "get_state"}
            usage_request = {"type": "get_session_stats"}
            if session.persistent_session is not None:
                state_request["id"] = self.state_id
                usage_request["id"] = self.usage_id
            session.proc.stdin.write(
                session.reader.encode(commands.PiCommand.from_wire(state_request))
            )
            session.proc.stdin.write(
                session.reader.encode(commands.PiCommand.from_wire(usage_request))
            )
            await session.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
