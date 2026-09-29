"""Coordination results: owned coordinator state and transitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Applied(Generic[T]):
    value: T


@dataclass(frozen=True, slots=True)
class AlreadyApplied(Generic[T]):
    value: T
