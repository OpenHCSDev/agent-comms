"""The original saved import observation, shared by its reader and writer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.execution.turn_context import HistoricalInstructionProvenance
from agent_comms.installer.harness_install import Harness
from agent_comms.nominal.field_codec import WireName


@dataclass(frozen=True, slots=True)
class ImportedSessionMetadata:
    """Original saved import observation; references grant reading, never execution."""

    harness: Annotated[type[Harness], WireName("format")]
    source_id: str
    messages_seen: int
    messages_imported: int
    truncated_messages: int
    notices: tuple[str, ...]
    historical_instructions: tuple[HistoricalInstructionProvenance, ...] = ()

    @classmethod
    def sources_for_owner(cls, registry, owner):
        """Original metadata membership borrows registry and selected-file ownership."""
        current = registry.require(owner.name)
        if current.incarnation != owner.incarnation:
            raise ValueError("Imported context thread incarnation changed")
        if current.session_file != owner.session_file:
            raise ValueError("Imported context selected session changed")
        return current.runtime.imported_sources()
