"""Owner and frontend change in lockstep: the built Core commit is the protocol.

Every socket line is a declared family member, encoded and decoded once by
FieldCodec. Each member answers what its reader does with it.
"""
from __future__ import annotations

import json
import typing
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Self, cast

from acp import RequestError
from acp.schema import DeniedOutcome, RequestPermissionResponse
from pydantic import BaseModel

from agent_comms import field_codec

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, MalformedRecord, Token256Text
from .package_stamp import STAMP

if TYPE_CHECKING:
    from .runtime import RuntimeProxy, SocketClient

CORE_COMMIT = STAMP.commit()


@dataclass(frozen=True, kw_only=True)
class SocketLine(DeclaredFamily, affix=""):
    """One JSON line on an owner socket; each direction reads only its own members."""

    @classmethod
    def read(cls, line: bytes) -> Self:
        return FieldCodec.decode(cls, FieldCodec.parse(line))

    def wire(self) -> bytes:
        return (json.dumps(FieldCodec.encode(self)) + "\n").encode()


@dataclass(frozen=True, kw_only=True)
class OwnerLine(SocketLine):
    """A line the owner writes to an attached client."""

    declared_name = None  # a direction, not a line

    def announced(self, thread: str) -> None:
        """The first subscription line; only this Core's announcement passes."""
        raise IncompatibleOwnerError(UnannouncedOwner(thread=thread))

    async def attach(self, proxy: RuntimeProxy) -> OwnerReady | None:
        """A line before ready; only ready completes the attachment."""
        await self.deliver(proxy)
        return None

    async def deliver(self, proxy: RuntimeProxy) -> None:
        raise MalformedRecord(f"The owner sent {self.declared_name} to an attached client.")

    def reply(self) -> object:
        raise MalformedRecord(f"The owner answered a request with {self.declared_name}.")


@dataclass(frozen=True, kw_only=True)
class OwnerAnnouncement(OwnerLine):
    """The first line of every owner subscription, compared before any update."""

    core_commit: str = CORE_COMMIT

    def announced(self, thread: str) -> None:
        if self != OwnerAnnouncement():
            raise IncompatibleOwnerError(
                OtherCoreOwner(thread=thread, core_commit=self.core_commit)
            )


class ControllerTokenText(Token256Text):
    """The owner issues each attachment a 32-byte hex controller token."""


@dataclass(frozen=True, kw_only=True)
class OwnerReady(OwnerLine):
    """The subscription line that completes attachment: session metadata and its controller."""

    ready: dict[str, object]
    controller_token: Annotated[str, ControllerTokenText, field_codec.WireName("controllerToken")]

    async def attach(self, proxy: RuntimeProxy) -> OwnerReady:
        return self


@dataclass(frozen=True, kw_only=True)
class OwnerUpdate(OwnerLine):
    """An ACP session update; its metadata is presented again for the attached session."""

    update: dict[str, object]
    meta: typing.Annotated[dict[str, object], field_codec.WireName("_meta")] = field(
        default_factory=dict
    )

    @classmethod
    def of(cls, update: BaseModel) -> Self:
        body = update.model_dump(by_alias=True, exclude_none=True)
        return cls(update=body, meta=cast(dict[str, object], body.pop("_meta", {})))

    async def deliver(self, proxy: RuntimeProxy) -> None:
        await proxy.present(self)


@dataclass(frozen=True, kw_only=True)
class OwnerPermissionRequest(OwnerLine):
    """A permission dialog for the attached controller, answered by its id."""

    id: str
    tool_call: typing.Annotated[dict[str, object], field_codec.WireName("toolCall")]
    options: list[dict[str, object]]

    async def deliver(self, proxy: RuntimeProxy) -> None:
        proxy.ask(self)


@dataclass(frozen=True, kw_only=True)
class OwnerResult(OwnerLine):
    """The single reply to a request that is not a subscription."""

    result: object

    def reply(self) -> object:
        return self.result


@dataclass(frozen=True)
class OwnerRpcError:
    code: int
    message: str
    data: object = None


@dataclass(frozen=True, kw_only=True)
class OwnerFailure(OwnerLine):
    """A failed request. A JSON-RPC error raises again as itself on the client."""

    error: str
    rpc_error: typing.Annotated[OwnerRpcError | None, field_codec.WireName("rpcError")] = field(
        default=None
    )

    @classmethod
    def of(cls, error: Exception) -> Self:
        if not isinstance(error, RequestError):
            return cls(error=str(error) or type(error).__name__)
        data = error.data if isinstance(error.data, dict) else {}
        reason = data.get("details") or data.get("reason")
        # The pi project extension shows the text; Core clients raise the RPC error.
        return cls(
            error=reason if isinstance(reason, str) and reason else str(error),
            rpc_error=OwnerRpcError(error.code, str(error), error.data),
        )

    def raised(self) -> Exception:
        if self.rpc_error is None:
            return RuntimeError(self.error)
        return RequestError(self.rpc_error.code, self.rpc_error.message, self.rpc_error.data)

    def announced(self, thread: str) -> None:
        raise self.raised()

    async def deliver(self, proxy: RuntimeProxy) -> None:
        raise self.raised()

    def reply(self) -> object:
        raise self.raised()


@dataclass(frozen=True, kw_only=True)
class ClientLine(SocketLine):
    """A line an attached client writes back to the owner."""

    declared_name = None  # a direction, not a line

    def answer(self, client: SocketClient) -> None:
        raise MalformedRecord(f"The client sent {self.declared_name} to the owner.")


@dataclass(frozen=True, kw_only=True)
class ClientPermissionResponse(ClientLine):
    """The controller's decision on one permission request."""

    id: str
    outcome: dict[str, object]

    def __post_init__(self) -> None:
        self.response()

    @classmethod
    def cancelled(cls, request_id: str) -> Self:
        denied = RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return cls.of(request_id, denied)

    @classmethod
    def of(cls, request_id: str, response: RequestPermissionResponse) -> Self:
        outcome = response.outcome.model_dump(by_alias=True, exclude_none=True)
        return cls(id=request_id, outcome=outcome)

    def response(self) -> RequestPermissionResponse:
        return RequestPermissionResponse.model_validate({"outcome": self.outcome})

    def answer(self, client: SocketClient) -> None:
        pending = client.pending.get(self.id)
        if pending is not None and not pending.done():
            pending.set_result(self.response())


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
        return (
            f"The {self.thread} owner runs {self.owner_core()}, not this app's Core "
            f"{CORE_COMMIT[:9]}. Its history is shown read-only; sending opens after "
            f"the owner restarts while idle."
        )

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
