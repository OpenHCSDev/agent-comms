"""Wake policy owns the initial disposition and engagement decision."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .coordination_errors import IntegrityViolationError
from .declared_family import DeclaredFamily


@dataclass(frozen=True)
class Engagement(DeclaredFamily, affix="Engagement"):
    execution_id: str
    exact_target: str

    def __post_init__(self):
        if not self.execution_id or not self.exact_target:
            raise IntegrityViolationError("engaged claim requires execution and exact target")

    @property
    @abstractmethod
    def verdict(self) -> str | None: ...


class FullEngagement(Engagement):
    @property
    def verdict(self):
        return None


class BoundedTriageEngagement(Engagement):
    @property
    def verdict(self):
        return "engage"


@dataclass(frozen=True)
class WakePolicy(DeclaredFamily, affix="Wake"):
    active: ClassVar[bool] = True
    triage: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def initial_disposition(cls) -> str: ...

    def engage(self, execution_id: str, target: str) -> Engagement:
        return Engagement.decode(self.declared_name)(execution_id, target)

    @property
    def engagement_verdict(self) -> str | None:
        return None


class PassiveWake(WakePolicy):
    active = False

    @classmethod
    def initial_disposition(cls):
        return "passive"

    def engage(self, execution_id, target):
        raise IntegrityViolationError("passive claim cannot engage")


class BoundedTriageWake(WakePolicy):
    triage = True

    @classmethod
    def initial_disposition(cls):
        return "triage_pending"

    @property
    def engagement_verdict(self):
        return "engage"


class FullWake(WakePolicy):
    @classmethod
    def initial_disposition(cls):
        return "full_pending"
