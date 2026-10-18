"""Owner and frontend change in lockstep: the built Core commit is the protocol."""
from __future__ import annotations

import re
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Annotated

from acp import RequestError

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, MalformedRecord, TextRepresentation
from .package_stamp import STAMP

CORE_COMMIT = STAMP.commit()


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


class ControllerTokenText(TextRepresentation):
    """The owner issues each attachment a 32-byte hex controller token."""

    @classmethod
    def encode(cls, value: object) -> object:
        return cls.decode(value)

    @classmethod
    def from_text(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise MalformedRecord("Owner subscription ready requires a valid controllerToken.")
        return value


@dataclass(frozen=True)
class OwnerReady:
    """The subscription line that completes attachment: session metadata and its controller."""

    ready: dict[str, object]
    controller_token: Annotated[str, ControllerTokenText] = field(
        metadata={"wire_name": "controllerToken"}
    )


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
                f"{CORE_COMMIT[:9]}. Its history is shown read-only; sending opens after "
                f"the owner restarts while idle.")

    async def request(self, action: str, **_: object) -> dict:
        """The history-only attachment refuses every owner request with this owner's line."""
        raise RequestError.invalid_params(self.failure_metadata())

    async def close(self) -> None:
        pass

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
