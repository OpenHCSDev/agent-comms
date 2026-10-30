"""The slots a session's subagents occupy, and the phase each one reports per tool call.

PreToolUse runs this on every subagent tool call, so it imports only the route attachment and
the activity store, never the Comms facade or the CLI (agent_comms_hooks/__init__.py).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.active_route import resolve_comms_route
from agent_comms.route_client import ActivityClient
from agent_comms.turn_phase import TurnPhase
from agent_comms_hooks import SubagentId
from agent_comms_hooks.watch import CommanderSession, WatchClaim, WatchRecord


class SubagentSeats(CommanderSession):
    """The slots this session's subagents occupy, one file per occupant naming its slot:
    Claude Code names the subagent and Core names the slot, so the pairing is written only
    here, when Core seats an occupant, and removed when Core vacates it."""

    command: ClassVar[tuple[str, ...]] = ("agent-comms-commander", "order")
    timeout: ClassVar[float] = 30

    @property
    def seats(self) -> Path:
        return WatchClaim(self.session_id).path.with_suffix(".slots")

    def run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)

    def core(self, record: WatchRecord, *argv: str) -> subprocess.CompletedProcess[str]:
        """Core on this session's bus; Core writes its answer, or its refusal, to stdout."""
        return self.run(sys.executable, "-m", "agent_comms.cli", "--root", record.root, *argv)

    def vacate(self, record: WatchRecord, seat: Path) -> tuple[str, ...]:
        """Core's refusal to vacate the seat's slot, or none once it is vacated."""
        run = self.core(record, "retire-slot", "--name", seat.read_text())
        if run.returncode:
            return (run.stdout.strip(),)
        seat.unlink()
        return ()


@dataclass(frozen=True)
class SeatedSubagent(SubagentSeats):
    """The existing seat or commander-fork name identifies whose activity a hook reports."""

    agent_id: SubagentId

    @property
    def seat(self) -> Path:
        return self.seats / self.agent_id

    def enter(self, phase: TurnPhase) -> None:
        state = WatchClaim(self.session_id).state()
        state.by_role(
            lambda: None,
            lambda record: self.record_phase(record, self.seat.read_text(), phase),
            lambda record: self.record_phase(record, self.agent_id.fork, phase),
        )

    @staticmethod
    def record_phase(record: WatchRecord, name: str, phase: TurnPhase) -> None:
        with ActivityClient.client(resolve_comms_route(record.root)) as client:
            client.agents.enter_phase(name, phase)
