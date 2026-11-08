"""Decoded native response facts consumed by request failures."""

from typing import Protocol


class NativeResponseErrorRead(Protocol):
    def text_or(self, otherwise: str, /) -> str: ...


class NativeResponseFailureRead(Protocol):
    @property
    def error(self) -> NativeResponseErrorRead: ...


class NativeOperationFailureRead(Protocol):
    @property
    def error(self) -> object: ...
