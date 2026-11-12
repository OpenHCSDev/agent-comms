"""Inspect historical imported context sources."""

from __future__ import annotations


from agent_comms.cli_support.context_option import ContextOptionCliCommand
from agent_comms.cli_verbs.cli_commands import ImportedContextSources
from agent_comms.comms import Comms
from agent_comms.execution.imported_session_metadata import ImportedSessionMetadata


class ImportedContextCliCommand(ContextOptionCliCommand):

    @classmethod
    def context_imported(cls) -> bool:
        return True

    def apply(self, ctx: Comms) -> ImportedContextSources:
        owner = ctx.registry.require(self.thread)
        return ImportedContextSources(
            "historical-imported-instructions; not current or recorded native context",
            ImportedSessionMetadata.sources_for_owner(ctx.registry, owner))

