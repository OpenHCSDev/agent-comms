"""Saved-session representation owns binding to the selected runtime."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .field_codec import SubclassUnion
from .thread_text import AbsentText, GivenText, TextChoice
from .unstarted_runtime import UnstartedRuntime

if TYPE_CHECKING:
    from .agent_backend import AgentBackend, RuntimeLocation


class SessionFile(SubclassUnion, TextChoice, affix="SessionFile"):
    """A thread's saved native session file, or null before its runtime first saves one."""

    @abstractmethod
    def location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        """The runtime this file binds under the thread's backend."""

    @abstractmethod
    def applied(self, previous: SessionFile) -> SessionFile:
        """The file a redeclaration keeps: its own, or the previous one."""


@dataclass(frozen=True, slots=True)
class SavedSessionFile(GivenText, SessionFile):
    text: str

    def location(self, backend: type[AgentBackend]) -> RuntimeLocation:
        return backend.location_type(self.text)

    def applied(self, previous: SessionFile) -> SessionFile:
        return self


@dataclass(frozen=True, slots=True)
class NoSessionFile(AbsentText, SessionFile):
    def location(self, backend: type[AgentBackend]) -> RuntimeLocation:

        return UnstartedRuntime(backend)

    def applied(self, previous: SessionFile) -> SessionFile:
        return previous
