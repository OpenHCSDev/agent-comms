"""Compare consecutive recorded context manifests."""

from __future__ import annotations

from itertools import islice
from typing import ClassVar

from agent_comms.cli_support.context_option import ContextOptionCliCommand
from agent_comms.comms import Comms
from agent_comms.execution.turn_context import ContextManifestChange


class DiffContextCliCommand(ContextOptionCliCommand):

    @classmethod
    def context_diff(cls) -> bool:
        return True

    def apply(self, ctx: Comms) -> ContextManifestChange:
        manifests = iter(self.manifests(ctx))
        for current in islice(manifests, 1):
            for previous in manifests:
                if not current.turn.same_recording(previous.turn):
                    return current.changed_since(previous)
            raise ValueError("No preceding recorded turn exists for comparison")
        raise ValueError("No original context manifest exists for the requested turn")

