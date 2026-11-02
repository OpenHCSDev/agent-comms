"""A fold whose members decide how they combine.

Folding parts into one answer usually has two kinds of member: one that accumulates parts
into the answer, and one that, once it appears, absorbs the whole fold (a failure, a change
that needs everything redone). Each family mixes in the member it is; the fold never asks
which member it holds.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Self


class Fold(ABC):
    @abstractmethod
    def joined(self, answer: Self) -> Self:
        """This part folded into the answer so far."""

    @abstractmethod
    def including(self, part: Self) -> Self:
        """The answer so far with an accumulating part added."""


class Accumulates(Fold):
    """A part the answer gathers: folding it in adds it to whatever the answer holds."""

    def joined(self, answer: Self) -> Self:
        return answer.including(self)


class Absorbs(Fold):
    """A part that decides the whole answer once it appears; later parts change nothing."""

    def joined(self, answer: Self) -> Self:
        return self.absorbing(answer)

    def absorbing(self, answer: Self) -> Self:
        """This part standing in for the answer it absorbed."""
        return self

    def including(self, part: Self) -> Self:
        return self
