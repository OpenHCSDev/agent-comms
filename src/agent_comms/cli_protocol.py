"""Declared JSON boundaries shared by Core's CLI and external CLI adapters."""
from __future__ import annotations

import asyncio
import builtins
from abc import ABC, abstractmethod
from dataclasses import replace
from graphlib import TopologicalSorter
from typing import TYPE_CHECKING, Annotated, AsyncIterator, ClassVar, NoReturn, Self, Union

from metaclass_registry import AutoRegisterMeta, RegistryConfig
from pydantic import BaseModel, ConfigDict, Discriminator, InstanceOf, PlainSerializer, PlainValidator, Tag, TypeAdapter, computed_field, field_validator, model_validator
from pydantic._internal._model_construction import ModelMetaclass
from typing_extensions import TypeForm

from .coordination_errors import CoordinationReadUnavailable
from .declared_family import DeclaredFamily
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


class WireModel(BaseModel):
    """Strict fields at an external protocol boundary."""
    model_config = ConfigDict(strict=True)


class TimestampedWire(WireModel):
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


class RegisteredWireMeta(AutoRegisterMeta, ModelMetaclass):
    """Compose declaration registration with external-field validation."""


class WireHeader(WireModel):
    type: str


class TextWire:
    """The text tag shared by external CLI content families."""

    wire_tag: ClassVar[str] = 'text'


class TaggedWire(WireModel, ABC, metaclass=RegisteredWireMeta):
    """A JSON type tag supplied by its declared class, not caller policy."""
    wire_tag: ClassVar[str]
    header: ClassVar[builtins.type[WireHeader]] = WireHeader

    @staticmethod
    def family_registry() -> RegistryConfig:
        return RegistryConfig(registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True)

    @computed_field
    @property
    def type(self) -> str:
        return self.wire_tag

    @classmethod
    def tag_of(cls, value: object) -> str:
        return cls.header.model_validate(value).type

    @classmethod
    def declared_union(cls) -> TypeForm[Self]:
        def tag(value: object) -> str:
            if isinstance(value, cls):
                return value.wire_tag
            return cls.tag_of(value)
        by_tag: dict[str, list[builtins.type[TaggedWire]]] = {}
        for member in cls.__registry_config__.registry_dict.values():
            by_tag.setdefault(member.wire_tag, []).append(member)
        variants = []
        for wire_tag, members in by_tag.items():
            hierarchy = TopologicalSorter({
                member: tuple(base for base in member.__mro__[1:] if base in members)
                for member in members
            })
            # An inherited specialization must precede its general parent.
            alternatives = Union[tuple(reversed(tuple(hierarchy.static_order())))]
            variants.append(Annotated[alternatives, Tag(wire_tag)])
        return Annotated[Union[tuple(variants)], Discriminator(tag)]


class OpenWire(TaggedWire, ABC):
    """A family whose source adds kinds freely: a tag it does not declare decodes as its
    member tagged 'foreign', which carries no conversation."""

    @classmethod
    def tag_of(cls, value: object) -> str:
        tag = super().tag_of(value)
        return tag if tag in cls.__registry_config__.registry_dict else 'foreign'


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


class WatchLine(TaggedWire):
    """One declared line from a Core watch child."""
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True,
    )

    @abstractmethod
    async def handle(self, bridge: CliBridge, child: AttachedChild) -> None: ...

    @staticmethod
    async def stream(child: AttachedChild) -> AsyncIterator[WatchLine]:
        async for line in child.stdout:
            yield WATCH_LINE.validate_json(line)


class CoreError(WatchLine):
    """One-line Core failure; each member owns its source and watch behavior."""
    exception_type: ClassVar[builtins.type[BaseException]]
    error: str

    @classmethod
    def from_exception(cls, error: BaseException) -> CoreError:
        members = {member.exception_type: member
                   for member in cls.__registry_config__.registry_dict.values()
                   if issubclass(member, cls)}
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


class UnavailableCoreError(CoreError):
    wire_tag = 'unavailable'
    exception_type = CoordinationReadUnavailable


class HeldCoreError(CoreError):
    """The bus went into maintenance: the child has closed, and the bridge resumes it."""
    wire_tag = 'held'
    exception_type = MaintenanceHeldError

    async def handle(self, bridge: CliBridge, child: AttachedChild) -> None:
        await child.wait()
        raise MaintenanceHeldError(self.error)


class FailedCoreError(CoreError):
    wire_tag = 'failed'
    exception_type = BaseException


class ArrivedMessage(WatchLine):
    """One decoded message; its journal retains canonical Message wire."""
    wire_tag = 'message'
    message: InstanceOf[Annotated[Message, PlainValidator(Message.from_wire), PlainSerializer(Message.to_wire)]]

    async def handle(self, bridge: CliBridge, child: AttachedChild) -> None:
        await bridge.accept(self.message)


class EarlierItems(WatchLine, TurnItem):
    """The pager's outstanding continuation, handled only by its socket-owning bridge."""
    wire_tag = 'earlier-items'
    request: RequestedEarlierPage

    @classmethod
    def from_row(cls, row):
        return cls(turn_id=row.turn_id, item_id=row.item_id, request=row.earlier)

    async def handle(self, bridge, child: AttachedChild) -> None:
        await bridge.earlier(self)


WATCH_LINE = TypeAdapter(WatchLine.declared_union())
