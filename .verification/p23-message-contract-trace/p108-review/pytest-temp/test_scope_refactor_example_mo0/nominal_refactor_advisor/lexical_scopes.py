from __future__ import annotations

from dataclasses import dataclass


def _argument_names(values):
    return set(values)


@dataclass(frozen=True)
class FunctionBindingProjection:
    names: frozenset[str]
    @classmethod
    def from_values(cls, values):
        return cls(frozenset(_argument_names(values)))


class ExistingScope: pass
