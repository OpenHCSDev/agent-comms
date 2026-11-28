"""Abstract inspection query contracts shared by AgentBackend and concrete runtimes."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BackendQuery(ABC):
    """An inspection query handled by backends that support native inspection."""


class BackendQueryResponse(ABC):
    """The response payload returned by a backend inspection query."""


class ContextCommitProof(ABC):
    """Corroborated model-context commit proof handed to InputStartListener."""


class TurnInputContent(ABC):
    """Prepared input payload handed to RunsTurns.run_turn."""

    text: str

    @abstractmethod
    def acp_prompt_blocks(self) -> list[dict[str, object]]:
        """Render this input as ACP prompt content blocks."""
