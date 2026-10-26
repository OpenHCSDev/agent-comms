"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_verbs.context_option import ContextOptionCliCommand
from agent_comms.comms import Comms
from agent_comms.turn_context import ContextManifestChange


class DiffContextCliCommand(ContextOptionCliCommand):
    def apply(self, ctx: Comms) -> ContextManifestChange:
        manifests = iter(self.manifests(ctx))
        try:
            current = next(manifests)
        except StopIteration:
            raise ValueError("No original context manifest exists for the requested turn") from None
        for previous in manifests:
            if not current.turn.same_recording(previous.turn):
                return current.changed_since(previous)
        raise ValueError("No preceding recorded turn exists for comparison")
