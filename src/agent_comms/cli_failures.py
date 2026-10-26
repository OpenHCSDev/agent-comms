from __future__ import annotations

from agent_comms.coordination_errors import CommandFailure


CLI_FAILURES = (ValueError, OSError, CommandFailure)
