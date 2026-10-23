"""A fact each class derives once from its own declaration and keeps in its own namespace."""

from __future__ import annotations

from collections.abc import Callable


class ClassFact[Owner, Value]:
    """Read through the class or any instance. On first read each class derives its own
    value by passing itself to the wrapped function, and stores it in its own
    ``__dict__``, so a subclass never reads its parent's, and the fact lives and dies
    with the class instead of in a cache beside it."""

    def __init__(self, derive: Callable[[type[Owner]], Value]) -> None:
        self.derive = derive
        self.__doc__ = derive.__doc__

    def __set_name__(self, owner: type[Owner], name: str) -> None:
        self.slot = f"_{name.lstrip('_')}_derived"

    def __get__(self, instance: object, owner: type[Owner]) -> Value:
        if self.slot not in owner.__dict__:
            setattr(owner, self.slot, self.derive(owner))
        value: Value = owner.__dict__[self.slot]
        return value
