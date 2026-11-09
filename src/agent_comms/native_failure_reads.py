"""Decoded native response facts consumed by request failures."""

from abc import ABC, abstractmethod


class NativeResponseErrorRead(ABC):
    __slots__ = ()

    @abstractmethod
    def text_or(self, otherwise: str, /) -> str: ...


class NativeResponseFailureRead(ABC):
    __slots__ = ()

    error: NativeResponseErrorRead


class NativeOperationFailureRead(ABC):
    __slots__ = ()

    error: str
