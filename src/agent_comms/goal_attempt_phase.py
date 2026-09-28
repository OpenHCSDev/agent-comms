"""Attempt transitions, distinct from a goal generation and from launch authority."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState


@dataclass(frozen=True)
class GoalAttemptPhase(DeclaredFamily, LifecycleState, affix="Attempt"):
    terminal: ClassVar[bool] = False
    launched: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[GoalAttemptPhase], ...]: ...


class ReservedAttempt(GoalAttemptPhase):
    @classmethod
    def successors(cls):
        return ClaimedAttempt, FailedAttempt, ResolvedAttempt


class ClaimedAttempt(GoalAttemptPhase):
    launched = True

    @classmethod
    def successors(cls):
        return FailedAttempt, SucceededAttempt, ResolvedAttempt


class FailedAttempt(GoalAttemptPhase):
    @classmethod
    def successors(cls):
        return (ResolvedAttempt,)


class SucceededAttempt(GoalAttemptPhase):
    terminal = True

    @classmethod
    def successors(cls):
        return ()


class ResolvedAttempt(GoalAttemptPhase):
    terminal = True

    @classmethod
    def successors(cls):
        return ()
