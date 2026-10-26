"""Inspect historical imported context sources."""

from __future__ import annotations

from agent_comms.cli_commands import ImportedContextSources
from agent_comms.comms import Comms
from agent_comms.importing import ImportedSessionMetadata

from agent_comms.cli_context import ContextOptionCliCommand

class ImportedContextCliCommand(ContextOptionCliCommand):
    def apply(self, ctx: Comms) -> ImportedContextSources:
        owner = ctx.registry.require(self.thread)
        return ImportedContextSources(
            "historical-imported-instructions; not current or recorded native context",
            ImportedSessionMetadata.sources_for_owner(ctx.registry, owner))

