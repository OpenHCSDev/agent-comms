"""Durable single-file store for scoped agent-settings overrides (`agent_settings.json`).

Separated from ``agent_settings.py`` so thread deletion, rename, and installer carry
consult this store without importing ``objectstate`` or ``rule_deal`` on light paths.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol

from agent_comms.registry.agent_settings_wire import AgentSettingsDocument
from agent_comms.registry.thread_owned_state import ThreadOwnedState
from agent_comms.storage.locked_store import DocumentChange
from agent_comms.storage.maintenance_barrier import CarriedDocument
from agent_comms.storage.store_files import DependentStateTier, SingleFileStore


class NamedThreadRecord(Protocol):
    name: str


@dataclass(frozen=True, slots=True)
class AgentSettingsStore(
    SingleFileStore,
    ThreadOwnedState,
    CarriedDocument[AgentSettingsDocument],
    DependentStateTier,
):
    """Persisted authored-only agent settings overrides, keyed by ``ScopeKey`` string."""

    filename: ClassVar[str] = "agent_settings.json"
    json_indent = 2
    record_type = AgentSettingsDocument

    def empty(self) -> AgentSettingsDocument:
        return AgentSettingsDocument()

    def remove_threads(self, threads: Sequence[NamedThreadRecord]) -> None:
        names = frozenset(thread.name for thread in threads)
        self.update(
            lambda document: DocumentChange.to(document.without_threads(names))
        )

    def rename_thread(self, old_name: str, new_name: str) -> int:
        def change(document: AgentSettingsDocument) -> DocumentChange[AgentSettingsDocument, int]:
            renamed, count = document.renamed_thread(old_name, new_name)
            return DocumentChange(renamed if count else document, count)

        return self.update(change)
