"""Declared JSON boundaries shared by Core's CLI and external CLI adapters."""
from __future__ import annotations

import asyncio
import builtins
import json
import sys
from abc import ABC, abstractmethod
from graphlib import TopologicalSorter
from typing import TYPE_CHECKING, Annotated, ClassVar, Never, Union

from metaclass_registry import AutoRegisterMeta, RegistryConfig
from pydantic import BaseModel, ConfigDict, Discriminator, InstanceOf, PlainSerializer, PlainValidator, Tag, TypeAdapter, computed_field

from .coordination_errors import CoordinationReadUnavailable
from .declared_family import DeclaredFamily
from .messages import Message

if TYPE_CHECKING:
    from .cli_bridges.shared import CliBridge, CoreConnection, LineMonitorBridge, ObservedMonitor


class WireModel(BaseModel):
    """Strict fields at an external protocol boundary."""
    model_config = ConfigDict(strict=True)


class RegisteredWireMeta(AutoRegisterMeta, type(WireModel)):
    """Compose declaration registration with external-field validation."""


class WireHeader(WireModel):
    type: str


class TaggedWire(WireModel, ABC, metaclass=RegisteredWireMeta):
    """A JSON type tag supplied by its declared class, not caller policy."""
    wire_tag: ClassVar[str]
    header: ClassVar[builtins.type[WireHeader]] = WireHeader

    @computed_field
    @property
    def type(self) -> str:
        return self.wire_tag

    @classmethod
    def members(cls) -> tuple[builtins.type[TaggedWire], ...]:
        return tuple(member for member in cls.__registry_config__.registry_dict.values()
                     if issubclass(member, cls))

    @classmethod
    def declared_union(cls) -> object:
        def tag(value: object) -> str:
            if isinstance(value, cls):
                return value.wire_tag
            return cls.header.model_validate(value).type
        by_tag: dict[str, list[builtins.type[TaggedWire]]] = {}
        for member in cls.members():
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


class WatchItem(ABC):
    """One item of a Core watch stream; it knows how a CLI bridge takes it."""

    @abstractmethod
    async def handle(self, bridge: CliBridge) -> None: ...


class Outcome(DeclaredFamily, affix='Outcome'):
    """Stored spellings stay the same; their owners perform confirmation."""

    @classmethod
    async def acknowledge(cls, core: CoreConnection, message: Message) -> None:
        """Pending transport outcomes cannot advance Core's read state."""

    @classmethod
    async def observe_monitor(cls, bridge: LineMonitorBridge, message: Message,
                              record: ObservedMonitor) -> None:
        await bridge.finish_delivery(message, DeliveredOutcome, record)


class FilteredOutcome(Outcome):
    @classmethod
    async def acknowledge(cls, core: CoreConnection, message: Message) -> None:
        await core.acknowledge(message)


class UnconfirmedOutcome(Outcome):
    pass


class DeliveredOutcome(FilteredOutcome):
    @classmethod
    async def observe_monitor(cls, bridge: LineMonitorBridge, message: Message,
                              record: ObservedMonitor) -> None:
        """Reopened history preserves the first confirmation record and time."""


class EmittedOutcome(UnconfirmedOutcome):
    pass


class WatchLine(TaggedWire, WatchItem):
    """One declared Core watch line, including its continuation behavior."""
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True,
    )

    @staticmethod
    async def read(child: asyncio.subprocess.Process) -> WatchLine:
        line = await child.stdout.readline()
        if not line:
            raise RuntimeError(f'Core watch ended, exit {await child.wait()}')
        return WATCH_LINE.validate_json(line)

    async def read_next(self, child: asyncio.subprocess.Process) -> WatchLine | None:
        return await self.read(child)


class CoreError(WatchLine):
    """One-line Core failure; each member owns its source and watch behavior."""
    exception_type: ClassVar[builtins.type[BaseException]]
    error: str

    @classmethod
    def from_exception(cls, error: BaseException) -> CoreError:
        members = {member.exception_type: member for member in cls.members()}
        message = str(error)
        cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        while cause is not None:
            message += f'; caused by {builtins.type(cause).__name__}: {cause}'
            cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
        for declaration in builtins.type(error).__mro__:
            if declaration in members:
                return members[declaration](error=message)
        raise TypeError('No declared Core error handles this exception')

    def raise_failure(self, operation: str) -> Never:
        raise RuntimeError(f'Core {operation}: {self.error}')

    async def read_next(self, child: asyncio.subprocess.Process) -> None:
        """An error is the final record, never another message subscription."""
        return None

    @abstractmethod
    def watch_delay(self) -> float:
        """Delay another read subscription, or raise for a permanent failure."""

    async def handle(self, bridge: CliBridge) -> None:
        """Core ended this read stream; wait before the bridge opens the next."""
        delay = self.watch_delay()
        print(f'Core watch unavailable: {self.error}; reconnecting read stream',
              file=sys.stderr, flush=True)
        await asyncio.sleep(delay)


class UnavailableCoreError(CoreError):
    wire_tag = 'unavailable'
    exception_type = CoordinationReadUnavailable

    def watch_delay(self) -> float:
        return 0.25


class FailedCoreError(CoreError):
    wire_tag = 'failed'
    exception_type = BaseException

    def watch_delay(self) -> float:
        self.raise_failure('watch')


class ArrivedMessage(WatchLine):
    """One decoded message; its journal retains canonical Message wire."""
    wire_tag = 'message'
    message: InstanceOf[Annotated[Message, PlainValidator(Message.from_wire), PlainSerializer(Message.to_wire)]]

    @property
    def payload(self) -> str:
        return json.dumps(self.message.to_wire(), separators=(',', ':')) + '\n'

    async def handle(self, bridge: CliBridge) -> None:
        message = self.message
        conversation = message.type.conversation_message
        outcome = UnconfirmedOutcome if conversation else FilteredOutcome
        if not bridge.journal.begin(message, self.payload, outcome):
            await bridge.journal.outcome(message).acknowledge(bridge.core, message)
            return
        if not conversation:
            await bridge.finish_delivery(message, outcome)
            return
        result = await bridge.deliver(message)
        await bridge.settle(message, result)
        print(f'{result.declared_name}: {message.message_id} from {message.sender}',
              file=sys.stderr, flush=True)


CORE_ERROR = TypeAdapter(CoreError.declared_union())
WATCH_LINE = TypeAdapter(WatchLine.declared_union())
