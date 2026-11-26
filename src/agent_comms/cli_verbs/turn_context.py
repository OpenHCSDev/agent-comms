"""Recorded manifests for one selected turn."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import takewhile

from agent_comms.cli_verbs.cli_verb import CliBus
from agent_comms.cli_support.context_option import SelectedContextCliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.execution.turn_context import ContextManifest


@dataclass(frozen=True)
class RecordedContextManifests(CliResult):
    manifests: tuple[ContextManifest, ...]
    text_recorded: bool


class TurnContextCliCommand(SelectedContextCliCommand):
    def apply(self, ctx: CliBus) -> RecordedContextManifests:
        selected = tuple(takewhile(lambda manifest: manifest.turn.matches_generation(self.turn),
                                   self.manifests(ctx)))
        if not selected:
            raise ValueError("No original context manifest exists for the requested turn")
        return RecordedContextManifests(
            tuple(reversed(selected)), all(manifest.public_text_recorded for manifest in selected))

