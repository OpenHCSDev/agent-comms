"""Lifecycle membership declared by the domain state hierarchies."""

from __future__ import annotations


class LifecycleCapability:
    @classmethod
    def require(cls, capability: type[LifecycleCapability], refusal: Exception) -> None:
        if not issubclass(cls, capability):
            raise refusal

    @classmethod
    def items_with[T](cls, capability: type[LifecycleCapability], *items: T) -> tuple[T, ...]:
        return items if issubclass(cls, capability) else ()


class Settled(LifecycleCapability):
    pass


class Terminal(Settled):
    @classmethod
    def successors(cls) -> tuple[()]:
        return ()


class Failed(LifecycleCapability):
    pass


class Pending(LifecycleCapability):
    pass


class Active(LifecycleCapability):
    pass


class Executing(LifecycleCapability):
    pass


class Unresolved(LifecycleCapability):
    pass


class Deferred(LifecycleCapability):
    pass


class Complete(Settled):
    pass


class Successful(Terminal):
    pass
