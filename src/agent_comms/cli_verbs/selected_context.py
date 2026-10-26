"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_verbs.context_option import ContextOptionCliCommand
from agent_comms.comms import Comms
from agent_comms.turn_context import ContextManifest
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import dropwhile


@dataclass(frozen=True, kw_only=True)
class SelectedContextCliCommand(ContextOptionCliCommand):
    turn: int

    def manifests(self, ctx: Comms) -> Iterator[ContextManifest]:
        return dropwhile(lambda manifest: not manifest.turn.matches_generation(self.turn),
                         super().manifests(ctx))
