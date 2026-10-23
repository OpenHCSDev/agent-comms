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

    def holder(self, owner: type[Owner]) -> type[Owner]:
        """The class whose namespace holds the value read through ``owner``."""
        return owner

    def __get__(self, instance: object, owner: type[Owner]) -> Value:
        holder = self.holder(owner)
        if self.slot not in holder.__dict__:
            setattr(holder, self.slot, self.derive(owner))
        value: Value = holder.__dict__[self.slot]
        return value

    def forget(self, owner: type[Owner]) -> None:
        """Drop the value read through ``owner``, so the next read derives it again."""
        holder = self.holder(owner)
        if self.slot in holder.__dict__:
            delattr(holder, self.slot)


class FamilyFact[Owner, Value](ClassFact[Owner, Value]):
    """A fact about the whole family: the class declaring it holds the one value,
    derived from whichever member reads it first, so no member keeps a copy of its own."""

    def __set_name__(self, owner: type[Owner], name: str) -> None:
        super().__set_name__(owner, name)
        self.root = owner

    def holder(self, owner: type[Owner]) -> type[Owner]:
        return self.root

    @staticmethod
    def forget_declared(member: type) -> None:
        """Forget every family fact ``member`` inherits, so the next read counts it."""
        inherited = [
            fact
            for base in member.__mro__
            for fact in vars(base).values()
            if isinstance(fact, FamilyFact)
        ]
        for fact in inherited:
            fact.forget(member)
