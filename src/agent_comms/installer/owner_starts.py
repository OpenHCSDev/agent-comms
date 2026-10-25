"""What starting each stopped owner did."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from agent_comms.declared_family import DeclaredFamily


@dataclass(frozen=True)
class OwnerStart(DeclaredFamily, affix="Owner"):
    """What starting one stopped owner did. A unit that cannot start is reported by name;
    the runtime and bus it would run on are installed either way."""

    name: str

    def require_success(self, refusal: type[Exception]) -> None:
        """A successful owner action needs no refusal."""

    @abstractmethod
    def lines(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class StartedOwner(OwnerStart):
    def lines(self) -> tuple[str, ...]:
        return (f"Started owner: {self.name}",)


@dataclass(frozen=True)
class RefusedOwner(OwnerStart):
    error: str

    def require_success(self, refusal: type[Exception]) -> None:
        raise refusal(self.error)

    def lines(self) -> tuple[str, ...]:
        return (f"Owner not started: {self.name}: {self.error}",)
