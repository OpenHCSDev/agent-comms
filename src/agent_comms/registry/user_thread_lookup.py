from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass


class UserThreadLookup[Value](ABC):
    """The registry's human selection, including a registry with no human yet."""

    @abstractmethod
    def map[Other](self, resolve: Callable[[Value], Other]) -> UserThreadLookup[Other]: ...

    @abstractmethod
    def existing(self) -> tuple[Value, ...]: ...

    @abstractmethod
    def or_create(self, create: Callable[[], Value]) -> Value: ...

    @abstractmethod
    def require(self, refusal: Exception) -> Value: ...


@dataclass(frozen=True, slots=True)
class FoundUserThread[Value](UserThreadLookup[Value]):
    value: Value

    def map[Other](self, resolve: Callable[[Value], Other]) -> UserThreadLookup[Other]:
        return FoundUserThread(resolve(self.value))

    def existing(self) -> tuple[Value, ...]:
        return (self.value,)

    def or_create(self, create: Callable[[], Value]) -> Value:
        return self.value

    def require(self, refusal: Exception) -> Value:
        return self.value


@dataclass(frozen=True, slots=True)
class NoUserThread[Value](UserThreadLookup[Value]):
    def map[Other](self, resolve: Callable[[Value], Other]) -> UserThreadLookup[Other]:
        return NoUserThread()

    def existing(self) -> tuple[Value, ...]:
        return ()

    def or_create(self, create: Callable[[], Value]) -> Value:
        return create()

    def require(self, refusal: Exception) -> Value:
        raise refusal
