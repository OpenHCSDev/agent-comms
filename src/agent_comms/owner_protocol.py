"""Owner and frontend change in lockstep: the Core commit (git archive export-subst) is the protocol."""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec

CORE_COMMIT = "$Format:%H$"


@dataclass(frozen=True)
class OwnerAnnouncement:
    """The first line of every owner subscription, compared before any update."""
    core_commit: str = CORE_COMMIT

    def require_same_core(self, thread: str, message: dict) -> None:
        if message.keys() != {"core_commit"}:
            raise IncompatibleOwnerError(UnannouncedOwner(thread=thread))
        owner = FieldCodec.decode(OwnerAnnouncement, message)
        if owner != self:
            raise IncompatibleOwnerError(OtherCoreOwner(thread=thread, core_commit=owner.core_commit))


@dataclass(frozen=True, kw_only=True)
class IncompatibleOwner(DeclaredFamily, affix="Owner"):
    thread: str
    FAILURE = "agentCommsIncompatibleOwner"

    @classmethod
    def from_failure(cls, data: object) -> IncompatibleOwner | None:
        if not isinstance(data, dict) or cls.FAILURE not in data:
            return None
        return FieldCodec.decode(cls, data.get(cls.FAILURE))

    def failure_metadata(self) -> dict:
        return {"reason": self.line(), self.FAILURE: FieldCodec.encode(self)}

    def line(self) -> str:
        return (f"The {self.thread} owner runs {self.owner_core()}, not this app's Core "
                f"{CORE_COMMIT[:9]}. It opens here after the owner restarts while idle.")

    @abstractmethod
    def owner_core(self) -> str: ...


@dataclass(frozen=True, kw_only=True)
class UnannouncedOwner(IncompatibleOwner):
    def owner_core(self) -> str:
        return "an older Core"


@dataclass(frozen=True, kw_only=True)
class OtherCoreOwner(IncompatibleOwner):
    core_commit: str

    def owner_core(self) -> str:
        return f"Core {self.core_commit[:9]}"


class IncompatibleOwnerError(Exception):  # not a RuntimeError: reconnect loops do not retry it
    def __init__(self, owner: IncompatibleOwner) -> None:
        super().__init__(owner.line())
        self.owner = owner
