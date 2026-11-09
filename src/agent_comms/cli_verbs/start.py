from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_verbs.restore_owner import RestoreOwnerCliCommand
from agent_comms.registry.threads import StartOwnerControl


@dataclass(frozen=True, kw_only=True)
class StartCliCommand(StartOwnerControl, RestoreOwnerCliCommand):
    help = 'Start thread'
