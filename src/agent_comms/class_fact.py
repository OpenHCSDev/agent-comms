"""A fact each class derives from its own declaration and keeps in its own namespace."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar


class Declarations:
    """How many family declarations exist. A fact derived from membership (a table's CHECK
    choices, a family's member list) is current only for the declarations it saw."""

    generation: ClassVar[int] = 0

    @classmethod
    def changed(cls) -> None:
        """A family gained or lost a member."""
        cls.generation += 1


@dataclass(frozen=True)
class DerivedFact[Value]:
    generation: int
    value: Value

    def current(self) -> bool:
        return self.generation == Declarations.generation


class ClassFact[Owner, Value]:
    """Read through the class or any instance. Each class derives its own value by passing
    itself to the wrapped function and stores it in its own ``__dict__``, so a subclass never
    reads its parent's, and the fact lives and dies with the class instead of in a cache beside
    it. A later declaration can change what a fact derives from, so the fact is derived again
    after one: production declares everything at import, before any read, and pays nothing."""

    def __init__(self, derive: Callable[[type[Owner]], Value]) -> None:
        self.derive = derive
        self.__doc__ = derive.__doc__

    def __set_name__(self, owner: type[Owner], name: str) -> None:
        self.slot = f"_{name.lstrip('_')}_derived"

    def __get__(self, instance: object, owner: type[Owner]) -> Value:
        if self.slot not in owner.__dict__ or not owner.__dict__[self.slot].current():
            setattr(owner, self.slot, DerivedFact(Declarations.generation, self.derive(owner)))
        fact: DerivedFact[Value] = owner.__dict__[self.slot]
        return fact.value
