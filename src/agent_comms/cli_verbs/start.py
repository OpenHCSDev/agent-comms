"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_verbs.restore_owner import RestoreOwnerCliCommand
from agent_comms.thread_execution import StartOwnerControl
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class StartCliCommand(StartOwnerControl, RestoreOwnerCliCommand):
    help = 'Start thread'
