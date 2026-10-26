"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_verbs.diff_context import DiffContextCliCommand
from agent_comms.cli_verbs.selected_context import SelectedContextCliCommand


class TurnDiffContextCliCommand(SelectedContextCliCommand, DiffContextCliCommand):
    """Start the shared diff walk at the requested turn."""
