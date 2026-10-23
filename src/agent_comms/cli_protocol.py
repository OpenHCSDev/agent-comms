"""Declared JSON boundaries shared by Core's CLI and external CLI adapters."""
from __future__ import annotations

import asyncio
import builtins
from abc import ABC, abstractmethod
from dataclasses import replace
from graphlib import TopologicalSorter
from itertools import groupby
from typing import TYPE_CHECKING, Annotated, AsyncIterator, ClassVar, NoReturn, Self, Union

from pydantic import Discriminator, InstanceOf, PlainSerializer, PlainValidator, Tag, computed_field, field_validator, model_validator
from typing_extensions import TypeForm

from agent_comms import wire_models
from .coordination_errors import CoordinationReadUnavailable
from .declared_family import DeclaredFamily, UnknownFamilyMember
from .maintenance_barrier import MaintenanceHeldError
from .messages import Message
from .field_codec import TimestampText
from .recorded_time import UnrecordedTimeError
from .external_transcript import RequestedEarlierPage
from .turn_item import TurnItem

if TYPE_CHECKING:
    from .child_process import AttachedChild
    from .cli_bridges.shared import CliBridge, DeliveryRow


async def raise_child_exit(child: AttachedChild, reason: str) -> NoReturn:
    await child.wait()
    tail = await child.stderr_tail(max_bytes=8192)
    raise ChildProcessError(f'Core watch ended, exit {child.returncode}\n{reason}\n{tail}')


class TimestampedWire(wire_models.WireModel):
    """A source record's original ISO timestamp, decoded before making any events."""

    timestamp: float

    @model_validator(mode='before')
    @classmethod
    def require_source_time(cls, value):
        if isinstance(value, dict) and value.get('timestamp') is None:
            raise UnrecordedTimeError(f'{cls.__name__} has no recorded timestamp')
        return value

    @field_validator('timestamp', mode='before')
    @classmethod
    def epoch(cls, value: str) -> float:
        return TimestampText.from_text(value).at.timestamp()


class WireHeader(wire_models.WireModel):
    type: str


class TextWire:
    """The text tag shared by external CLI content families."""


class TaggedWire(wire_models.DeclaredWireFamily, wire_models.WireModel, ABC, affix="", declared_name=None):
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
    def tag_of(cls, value: object) -> str:
        return cls.header.model_validate(value).type

    @classmethod
    def declared_union(cls) -> TypeForm[Self]:
        def tag(value: object) -> str:
            if isinstance(value, cls):
                return value.tag()
            return cls.tag_of(value)
        variants = []
        declarations = sorted(cls.members_with(cls), key=lambda member: member.tag())
        for wire_tag, group in groupby(declarations, key=lambda member: member.tag()):
            members = tuple(group)
            hierarchy = TopologicalSorter({
                member: tuple(base for base in member.__mro__[1:] if base in members)
                for member in members
            })
            # An inherited specialization must precede its general parent.
            alternatives = Union[tuple(reversed(tuple(hierarchy.static_order())))]
            variants.append(Annotated[alternatives, Tag(wire_tag)])
        return Annotated[Union[tuple(variants)], Discriminator(tag)]


class OpenWire(TaggedWire, ABC, declared_name=None):
    """A family whose source adds kinds freely: a tag it does not declare decodes as its
    member tagged 'foreign', which carries no conversation."""

    @classmethod
    def tag_of(cls, value: object) -> str:
        tag = super().tag_of(value)
        try:
            cls.member_named(tag)
        except UnknownFamilyMember:
            return 'foreign'
        return tag


class Outcome(DeclaredFamily, affix='Outcome'):
    """Stored spellings stay the same; their owners perform confirmation."""

    @classmethod
    async def acknowledge(cls, bridge: CliBridge, message: Message) -> None:
        """Pending transport outcomes cannot advance Core's read state."""

    @classmethod
    async def finish(cls, bridge: CliBridge, delivery: DeliveryRow, observation: type[Outcome]) -> None:
        await observation.acknowledge(bridge, delivery.message)
        with bridge.db:
            replace(delivery, outcome=observation).upsert(bridge.db)


class FilteredOutcome(Outcome):
    @classmethod
    async def finish(cls, bridge: CliBridge, delivery: DeliveryRow, observation: type[Outcome]) -> None:
        """Core already confirmed this row; a later emission cannot acknowledge it twice."""

    @classmethod
    async def acknowledge(cls, bridge: CliBridge, message: Message) -> None:
        await asyncio.to_thread(bridge.comms.messaging.acknowledge, bridge.binding.bus, (message.reference,))


class UnconfirmedOutcome(Outcome):
    pass


class DeliveredOutcome(FilteredOutcome):
    pass


class EmittedOutcome(UnconfirmedOutcome):
    pass


class WatchLine(TaggedWire, affix=""):
    """One declared line from a Core watch child."""

    @abstractmethod
    async def handle(self, bridge: CliBridge, child: AttachedChild) -> None: ...

    @staticmethod
    async def stream(child: AttachedChild) -> AsyncIterator[WatchLine]:
        async for line in child.stdout:
            yield WATCH_LINE.validate_json(line)


class CoreError(WatchLine, declared_name=None):
    """One-line Core failure; each member owns its source and watch behavior."""
    exception_type: ClassVar[builtins.type[BaseException]]
    error: str

    @classmethod
    def from_exception(cls, error: BaseException) -> CoreError:
        members = {member.exception_type: member
                   for member in cls.members_with(cls)}
        message = str(error)
        cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        while cause is not None:
            message += f'; caused by {builtins.type(cause).__name__}: {cause}'
            cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
        for declaration in builtins.type(error).__mro__:
            if declaration in members:
                return members[declaration](error=message)
        raise TypeError('No declared Core error handles this exception')

    async def handle(self, bridge: CliBridge, child: AttachedChild) -> None:
        await raise_child_exit(child, self.error)


class UnavailableCoreError(CoreError, declared_name='unavailable'):
    exception_type = CoordinationReadUnavailable


class HeldCoreError(CoreError, declared_name='held'):
    """The bus went into maintenance: the child has closed, and the bridge resumes it."""
    exception_type = MaintenanceHeldError

    async def handle(self, bridge: CliBridge, child: AttachedChild) -> None:
        await child.wait()
        raise MaintenanceHeldError(self.error)


class FailedCoreError(CoreError, declared_name='failed'):
    exception_type = BaseException


class ArrivedMessage(WatchLine, declared_name='message'):
    """One decoded message; its journal retains canonical Message wire."""
    message: InstanceOf[Annotated[Message, PlainValidator(Message.from_wire), PlainSerializer(Message.to_wire)]]

    async def handle(self, bridge: CliBridge, child: AttachedChild) -> None:
        await bridge.accept(self.message)


class EarlierItems(WatchLine, TurnItem, declared_name='earlier-items'):
    """The pager's outstanding continuation, handled only by its socket-owning bridge."""
    request: RequestedEarlierPage

    @classmethod
    def from_row(cls, row):
        return cls(turn_id=row.turn_id, item_id=row.item_id, request=row.earlier)

    async def handle(self, bridge, child: AttachedChild) -> None:
        await bridge.earlier(self)


WATCH_LINE = wire_models.WireAdapter(WatchLine.declared_union())
