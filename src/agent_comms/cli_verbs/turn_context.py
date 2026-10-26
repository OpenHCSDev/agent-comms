"""Recorded manifests for one selected turn."""

from __future__ import annotations

from agent_comms.cli_commands import RecordedContextManifests
from agent_comms.comms import Comms
from itertools import takewhile

from agent_comms.cli_context import SelectedContextCliCommand

class TurnContextCliCommand(SelectedContextCliCommand):
    def apply(self, ctx: Comms) -> RecordedContextManifests:
        selected = tuple(takewhile(lambda manifest: manifest.turn.matches_generation(self.turn),
                                   self.manifests(ctx)))
        if not selected:
            raise ValueError("No original context manifest exists for the requested turn")
        return RecordedContextManifests(
            tuple(reversed(selected)), all(manifest.public_text_recorded for manifest in selected))

