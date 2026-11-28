"""Core's minimal turn-delivery surface shared by direct ACP backends without UI/ACP cycles."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.agent_backend import InputStartListener
from agent_comms.execution.send_outcome import SendBoundary
from agent_comms.execution.turn_inbox import TurnInbox


@dataclass(frozen=True, kw_only=True)
class DirectTurnDelivery:
    """Turn-scoped delivery bindings needed by direct streaming backends."""

    worktree: str
    inbox: TurnInbox
    send_boundary: SendBoundary
    input_start: InputStartListener
