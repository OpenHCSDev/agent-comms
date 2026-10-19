"""Every agent runtime Core reaches, as one family with two branches by who drives it.

An owned runtime is a process Core starts and whose turns it drives (Pi today). An external
runtime is a CLI the person runs (Claude Code, Codex, OpenCode): Core observes its transcript and
acts through its hooks and tools. What a runtime can do is a capability mixin shared across both
branches; a runtime without a capability does not inherit it, and Core asks the capability, never
which runtime it is talking to.

This module is light: runtimes import it, it imports no runtime.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli_protocol import Outcome
    from .messages import Message
    from .runtime_updates import RuntimeUpdate


class AgentRuntime:
    """One agent runtime Core reaches."""


class OwnedRuntime(AgentRuntime):
    """Core starts the runtime's process and drives its turns."""


class ExternalRuntime(AgentRuntime):
    """The person runs it; Core observes its transcript and acts through hooks and tools."""


class ReadsHistory(ABC):
    """Reads a session's past, oldest first."""

    @abstractmethod
    def history(self) -> AsyncIterator[RuntimeUpdate]: ...


class PublishesTranscript(ABC):
    """Reports the turns and items the runtime produces as it produces them."""

    @abstractmethod
    def transcript(self) -> AsyncIterator[RuntimeUpdate]:
        """Observe the runtime's output without submitting inputs or executing tools."""


class DeliversInput(ABC):
    """Places a bus message in front of the runtime's model."""

    @abstractmethod
    async def deliver(self, message: Message) -> type[Outcome]:
        """Return the runtime's evidence of delivery; the caller owns completion."""
