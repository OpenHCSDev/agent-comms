"""Which running owners one restart retires and relaunches."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_comms.field_codec import NullMember, SingleFieldValue, SubclassUnion

if TYPE_CHECKING:
    from agent_comms.registry_document import RegistrySnapshot
    from agent_comms.threads import Thread


class RestartScope(ABC):
    """The scope owns the threads it selects from one registry snapshot."""

    @abstractmethod
    def threads(self, snapshot: RegistrySnapshot) -> list[Thread]: ...


class NamedRestart(RestartScope):
    """Exactly the named running threads, aliases resolved once each."""

    @abstractmethod
    def names(self) -> tuple[str, ...]: ...

    def threads(self, snapshot: RegistrySnapshot) -> list[Thread]:
        return list({snapshot.require_active(name).name: snapshot.require_active(name)
                     for name in self.names()}.values())


@dataclass(frozen=True)
class OwnersNamed(NamedRestart):
    selected: tuple[str, ...]

    def names(self) -> tuple[str, ...]:
        return self.selected


class RestartTarget(SubclassUnion, RestartScope, affix="Owner"):
    """The command line's restart scope: one thread's name, or null for every running owner."""


@dataclass(frozen=True)
class OneOwner(SingleFieldValue, NamedRestart, RestartTarget):
    text: str

    def names(self) -> tuple[str, ...]:
        return (self.text,)


@dataclass(frozen=True)
class AllOwners(NullMember, RestartTarget):
    def threads(self, snapshot: RegistrySnapshot) -> list[Thread]:
        return [candidate for thread in snapshot.threads.values()
                for candidate in thread.restart_candidates(snapshot.status(thread.name))]
