"""Declared JSON boundaries shared by Core's CLI and external CLI adapters."""

from __future__ import annotations

import builtins
from abc import ABC
from collections.abc import Mapping
from graphlib import TopologicalSorter
from itertools import groupby
from typing import Annotated, ClassVar, Self, Union

from pydantic import (
    Discriminator,
    GetCoreSchemaHandler,
    InstanceOf,
    PlainSerializer,
    PlainValidator,
    Tag,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_core import CoreSchema
from typing_extensions import TypeForm

from .coordination_errors import CoordinationReadUnavailable
from .declared_family import DeclaredFamily, UnknownFamilyMember
from .external_transcript import RequestedEarlierPage
from .field_codec import TimestampText
from .maintenance_barrier import MaintenanceHeldError
from .messages import Message
from .recorded_time import UnrecordedTimeError
from .turn_item import NativeTurnId, TurnItem
from .wire_models import DeclaredWireFamily, WireModel

class TimestampedWire(WireModel):
    """A source record's original ISO timestamp, decoded before making any events."""

    timestamp: float

    @model_validator(mode="before")
    @classmethod
    def require_source_time(cls, value):
        if isinstance(value, dict) and value.get("timestamp") is None:
            raise UnrecordedTimeError(f"{cls.__name__} has no recorded timestamp")
        return value

    @field_validator("timestamp", mode="before")
    @classmethod
    def epoch(cls, value: str) -> float:
        return TimestampText.from_text(value).at.timestamp()


class WireHeader(WireModel):
    type: str


class TextWire:
    """The text tag shared by external CLI content families."""


class TaggedWire(DeclaredWireFamily, WireModel, ABC, affix="", declared_name=None):
    """A JSON type tag supplied by its declared class, not caller policy."""

    header: ClassVar[builtins.type[WireHeader]] = WireHeader

    @classmethod
    def tag(cls) -> str:
        return cls.declared_name

    @computed_field
    @property
    def type(self) -> str:
        return self.tag()

    @classmethod
    def tag_of(cls, value: Mapping[str, object] | TaggedWire) -> str:
        if isinstance(value, Mapping):
            return cls.header.model_validate(value).type
        return value.type

    @classmethod
    def declared_union(cls) -> TypeForm[Self]:
        variants = []
        declarations = sorted(cls.members_with(cls), key=lambda member: member.tag())
        for wire_tag, group in groupby(declarations, key=lambda member: member.tag()):
            members = tuple(group)
            hierarchy = TopologicalSorter(
                {
                    member: tuple(base for base in member.__mro__[1:] if base in members)
                    for member in members
                }
            )
            # An inherited specialization must precede its general parent.
            alternatives = Union[*reversed(tuple(hierarchy.static_order()))]
            variants.append(Annotated[alternatives, Tag(wire_tag)])
        return Annotated[Union[*variants], Discriminator(cls.tag_of)]


class DeclaredMembers:
    """Decodes the annotated family as the union of its declared members."""

    def __get_pydantic_core_schema__(
        self, family: type[TaggedWire], handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return handler(family.declared_union())


type Declared[Family: TaggedWire] = Annotated[Family, DeclaredMembers()]


class OpenWire(TaggedWire, ABC, declared_name=None):
    """A family whose source adds kinds freely: a tag it does not declare decodes as its
    member tagged 'foreign', which carries no conversation."""

    @classmethod
    def tag_of(cls, value: Mapping[str, object] | TaggedWire) -> str:
        tag = super().tag_of(value)
        try:
            cls.member_named(tag)
        except UnknownFamilyMember:
            return "foreign"
        return tag


class WatchLine(TaggedWire, affix=""):
    """One declared line of the agent-comms watch command's output."""


class CoreError(WatchLine, declared_name=None):
    """One-line Core failure; each member owns its source and watch behavior."""

    exception_type: ClassVar[builtins.type[BaseException]]
    error: str

    @classmethod
    def from_exception(cls, error: BaseException) -> CoreError:
        members = {member.exception_type: member for member in cls.members_with(cls)}
        message = str(error)
        cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        while cause is not None:
            message += f"; caused by {builtins.type(cause).__name__}: {cause}"
            cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
        for declaration in builtins.type(error).__mro__:
            if declaration in members:
                return members[declaration](error=message)
        raise TypeError("No declared Core error handles this exception")


class UnavailableCoreError(CoreError, declared_name="unavailable"):
    exception_type = CoordinationReadUnavailable


class HeldCoreError(CoreError, declared_name="held"):
    """The bus went into maintenance."""

    exception_type = MaintenanceHeldError


class FailedCoreError(CoreError, declared_name="failed"):
    exception_type = BaseException


class ArrivedMessage(WatchLine, declared_name="message"):
    """One decoded message; its journal retains canonical Message wire."""

    message: InstanceOf[
        Annotated[
            Message,
            PlainValidator(Message.from_wire),
            PlainSerializer(lambda message: message.to_wire()),
        ]
    ]


class EarlierItems(WatchLine, TurnItem, declared_name="earlier-items"):
    """The pager's outstanding continuation, handled only by its socket-owning bridge."""

    turn_id: NativeTurnId
    request: RequestedEarlierPage

    @classmethod
    def from_row(cls, row):
        return cls(turn_id=row.turn_id, item_id=row.item_id, request=row.earlier)

    async def handle(self, bridge) -> None:
        await bridge.earlier(self)


WATCH_LINE = WireModel.adapter(WatchLine.declared_union())
