"""Declared JSON boundaries shared by Core's CLI and external CLI adapters."""
from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from graphlib import TopologicalSorter
from typing import Annotated, ClassVar, Never, Union

from metaclass_registry import AutoRegisterMeta, RegistryConfig
from pydantic import BaseModel, ConfigDict, Discriminator, Tag, TypeAdapter, computed_field

from .coordination_errors import CoordinationReadUnavailable


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
    def declared_union(cls) -> object:
        def tag(value: object) -> str:
            if isinstance(value, cls):
                return value.wire_tag
            return cls.header.model_validate(value).type
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


class CoreError(TaggedWire, ABC):
    """One-line Core failure; each member owns its source and watch behavior."""
    __registry_config__: ClassVar[RegistryConfig] = RegistryConfig(
        registry_dict={}, key_attribute='wire_tag', skip_if_no_key=True,
    )
    exception_type: ClassVar[builtins.type[BaseException]]
    error: str

    @classmethod
    def from_exception(cls, error: BaseException) -> CoreError:
        members = {member.exception_type: member
                   for member in cls.__registry_config__.registry_dict.values()}
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

    @abstractmethod
    def watch_delay(self) -> float:
        """Delay another read subscription, or raise for a permanent failure."""


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


CORE_ERROR = TypeAdapter(CoreError.declared_union())
