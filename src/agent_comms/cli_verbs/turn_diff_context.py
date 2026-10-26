"""Compare manifests starting at the selected turn."""

from __future__ import annotations


from agent_comms.cli_context import SelectedContextCliCommand
from agent_comms.cli_verbs.diff_context import DiffContextCliCommand

class TurnDiffContextCliCommand(SelectedContextCliCommand, DiffContextCliCommand):
    """Start the shared diff walk at the requested turn."""

