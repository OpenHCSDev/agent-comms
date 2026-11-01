"""Wire values shared by Core observations and the Pi payload family below them."""

from __future__ import annotations

from abc import abstractmethod
import re
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, ClassVar, Literal, Self
from pydantic import JsonValue
from .nominal.declared_family import DeclaredFamily, UnknownFamilyMember
from .nominal.field_codec import ExternalField, FieldCodec, InputId, MalformedRecord, NullMember, RecordProblems, SingleFieldValue, ValidatedScalar, WireName, WireNullDefault, WireOmitDefault, boundary_decoder
from .pi_counts import NonnegativePiCount, TotalPiCount, UnreportedCount
from .turn_item import PiRequestKey, UsageChargeId
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pi_payloads import PiContent
    from .pending_requests import PendingRequest, PendingRequests
    from .pi_commands import PiCommand
    from .pi_events import Response
    from .turn_phase import TurnPhase


class PiPayload(ExternalField):
    wire_tag: ClassVar[str | None] = None

    @classmethod
    def decode_external_value(cls, value: object, record: Mapping[str, object]) -> object:
        if not isinstance(value, dict):
            raise MalformedRecord(f"Expected {cls.__name__} object")
        member = cls.wire_member(value)
        return FieldCodec.construct(member, member.decode_fields(value, {}))

    @classmethod
    def wire_member(cls, value: dict[str, JsonValue]) -> type[Self]:
        return cls

    @classmethod
    @boundary_decoder
    def decode_fields(
        cls, value: dict[str, JsonValue], decoded: dict[str, object],
    ) -> dict[str, object]:
        hints = FieldCodec._types(cls)
        problems: dict[str, str] = {}
        for f, key in FieldCodec.record_fields(cls):
            if f.name in decoded:
                decoded[f.name] = FieldCodec.decode_resolved(hints[f.name], decoded[f.name])
                continue
            plan = FieldCodec.field_plan(cls, f.name)
            try:
                captured = plan.decoded_external(value)
                if f.name not in captured and (plan.constructor_required or plan.required):
                    raise MalformedRecord(f"Missing required field {key}")
                decoded.update(captured)
            except MalformedRecord as refused:
                plan.locate(problems, refused)
        if problems:
            raise RecordProblems(problems)
        return decoded

    @classmethod
    def from_wire(cls, value):
        return FieldCodec.decode_external(cls, value, {})

    def to_wire(self):
        data = FieldCodec.encode_fields(self)
        if self.wire_tag is not None and DeclaredFamily.record_tag(self):
            data[self.wire_tag] = self.wire_discriminator()
        return data

    def wire_discriminator(self):
        """Refined internal members borrow their original external declaration."""
        return FieldCodec.encode(type(self))


class OpenPayload(PiPayload, DeclaredFamily):
    """An external tag selects its member or the family's declared unknown payload."""

    wire_tag = "type"

    @classmethod
    def wire_member(cls, value: dict[str, JsonValue]) -> type[Self]:
        try:
            member = cls.member_named(FieldCodec.decode(str, value.get(cls.wire_tag)))
            return member.wire_case(value)
        except (UnknownFamilyMember, MalformedRecord):
            member, = cls.members_with(UnknownPayload)
            return member

    @classmethod
    def wire_case(cls, value: dict[str, JsonValue]) -> type[Self]:
        return cls


PiValue = typing.TypeVar("PiValue")


PiField = Annotated[PiValue, WireOmitDefault()]


class PiReported[T](SingleFieldValue):
    """A value Pi sent in one of its optional fields; PiOmitted answers the same questions."""

    __slots__ = ()
    reported: ClassVar[bool] = True
    value: T

    @classmethod
    def from_wire(cls, data: object) -> Self:
        """The value decodes and encodes as the field it fills, family tag included."""
        annotation = cls.wire_field().annotation
        return cls(FieldCodec.decode_external(annotation, data, {}))

    def to_wire(self) -> object:
        return FieldCodec.encode(self.value, self.wire_field().annotation)

    def value_or[D](self, default: D) -> T | D:
        return self.value

    def present(self) -> tuple[T]:
        return (self.value,)

    def require(self) -> T:
        return self.value

    def preserve_evidence(self, raw: object) -> Self:
        return self


@dataclass(frozen=True, slots=True)
class PiOmitted(NullMember):
    """Pi left an optional field out or sent null; it goes back out the same way."""

    reported: ClassVar[bool] = False

    @property
    def message_parts(self) -> tuple[PiContent, ...]:
        return ()

    def rendered_text(self, separator: str) -> str:
        return ""

    def preserve_evidence(self, raw: object) -> Self:
        return self

    def value_or[D](self, default: D) -> D:
        return default

    def text_or[D](self, default: D) -> D:
        return default

    def require(self) -> typing.NoReturn:
        raise MalformedRecord("Pi left out a field this record requires")

    @classmethod
    @boundary_decoder
    def from_input[T](cls, value: T | None) -> T | PiOmitted:
        """Our own unset value reaches Pi's wire as Pi's omission."""
        return cls() if value is None else value


@dataclass(frozen=True)
class RequestId(PiReported[PiRequestKey]):
    """The id a command carries and Pi echoes on the response that answers it."""

    value: PiRequestKey

    def track_in[Command: PiCommand](
        self, pending: PendingRequests, owner: type[PiCommand], command: Command,
    ) -> PendingRequest[Command]:
        return pending.add(owner, self.value, request=command)

    def take_from(
        self, pending: PendingRequests, owner: type[PiCommand], response: Response,
    ) -> PiCommand | None:
        return pending.take(owner, self.value, response)


@dataclass(frozen=True, slots=True)
class AnonymousRequest(PiOmitted):
    """The record carried no id; its response is matched by command family alone."""

    def track_in[Command: PiCommand](
        self, pending: PendingRequests, owner: type[PiCommand], command: Command,
    ) -> PendingRequest[Command]:
        return pending.add(owner, PiRequestKey.fresh(), request=command)

    def take_from(
        self, pending: PendingRequests, owner: type[PiCommand], response: Response,
    ) -> PiCommand | None:
        return pending.take_anonymous(owner, response)

    def require(self) -> PiRequestKey:
        raise ValueError("A correlated native input requires its command ID")


PiRequestId = RequestId | AnonymousRequest


class CostAmount(ValidatedScalar):
    @classmethod
    def decode(cls, value: object) -> float:
        amount = FieldCodec.decode(float, value)
        if amount < 0:
            raise MalformedRecord("Pi cost must be a finite nonnegative number.")
        return amount

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(float)


PiCostAmount = Annotated[float, CostAmount]


@dataclass(frozen=True)
class PiCost(PiPayload):
    input: PiField[PiCostAmount] = 0.0
    output: PiField[PiCostAmount] = 0.0
    cache_read: Annotated[PiField[PiCostAmount], WireName("cacheRead")] = 0.0
    cache_write: Annotated[PiField[PiCostAmount], WireName("cacheWrite")] = 0.0
    total: PiField[PiCostAmount] = 0.0

    def charged(self) -> Decimal:
        return Decimal(str(self.total))


@dataclass(frozen=True)
class PiUsage(PiPayload):
    input: PiField[NonnegativePiCount] = UnreportedCount()
    output: PiField[NonnegativePiCount] = UnreportedCount()
    cache_read: Annotated[
        PiField[NonnegativePiCount], WireName("cacheRead")
    ] = UnreportedCount()
    cache_write: Annotated[
        PiField[NonnegativePiCount], WireName("cacheWrite")
    ] = UnreportedCount()
    total_tokens: Annotated[
        PiField[TotalPiCount], WireName("totalTokens")
    ] = UnreportedCount()
    cost: Annotated[PiField[PiCost], WireNullDefault()] = (
        PiCost()
    )
    reasoning: PiField[NonnegativePiCount] = UnreportedCount()
    cache_write_1h: Annotated[
        PiField[NonnegativePiCount], WireName("cacheWrite1h")
    ] = UnreportedCount()

    def charged(self, identity: UsageChargeId | PiOmitted) -> tuple[UsageChargeId, ...]:
        # A failed terminal may have usage counters but no provider response.
        # Only an observed identity can name a provider billing entry.
        return tuple(identity.present())


@dataclass(frozen=True)
class PiDiagnostic(OpenPayload, affix="Diagnostic"):
    provider_connection_failure: ClassVar[bool] = False

    def refine_model_request_failure(self, failure):
        return failure

    def turn_phase(self, phase: TurnPhase) -> TurnPhase:
        return phase

    @property
    def description(self) -> str:
        return ""


@dataclass(frozen=True)
class McpServerReceipt:
    id: str
    scope: Literal["user", "project"]
    state: type[McpServerState]
    calls: type[McpCallPolicy]
    tools: int
    resources: int
    prompts: int

    def __post_init__(self):
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", self.id) is None:
            raise ValueError("Invalid external MCP server identity")
        counts = self.tools, self.resources, self.prompts
        if any(not 0 <= count <= 10_000 for count in counts):
            raise ValueError("Invalid external MCP server counts")
        self.state.validate(self.calls, counts)


@dataclass(frozen=True)
class McpLiveAcknowledgement:
    version: Literal[1]
    source: Literal["pi-mcp-client"]
    input_id: Annotated[InputId, WireName("inputId")]
    state: Literal["running"]
    lifetime: Literal["turn"]
    servers: tuple[McpServerReceipt, ...]

    @classmethod
    def from_status(cls, text: str | None, input_id: InputId) -> McpLiveAcknowledgement | None:
        """Decode the package's external status claim once at its boundary.

        Same-user extensions can imitate this claim. It is never a package
        attestation or permission, even when the current input identity matches.
        """
        if text is None or len(text) > 8192:
            return None
        try:
            acknowledgement = FieldCodec.decode(cls, FieldCodec.parse(text))
        except MalformedRecord:
            return None
        return acknowledgement if acknowledgement.input_id == input_id else None

    def __post_init__(self):
        if len(self.servers) > 32 or len({server.id for server in self.servers}) != len(
            self.servers
        ):
            raise ValueError("Invalid external MCP server collection")


@dataclass(frozen=True, kw_only=True)
class Opaque(PiPayload):
    """Own the original external JSON value and its normalization."""

    payload: JsonValue

    @classmethod
    def decode_fields(
        cls, value: dict[str, JsonValue], decoded: dict[str, object],
    ) -> dict[str, object]:
        return super().decode_fields(value, dict(
            decoded, payload=FieldCodec.decode(JsonValue, value),
        ))

    def to_wire(self) -> JsonValue:
        return self.payload


class UnknownPayload(Opaque):
    """The declaration an open family selects for an unknown external tag."""


class McpCallPolicy(DeclaredFamily, affix="McpCallPolicy"):
    @classmethod
    def require_available(cls):
        pass

    @classmethod
    def require_unavailable(cls):
        raise ValueError("Inactive MCP servers cannot advertise calls")


class McpServerState(DeclaredFamily, affix="McpServerState"):
    @classmethod
    @abstractmethod
    def validate(cls, calls: type[McpCallPolicy], counts: tuple[int, ...]): ...
