"""Goal progress owns its report, display and treatment of runtime notes."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, fields
from typing import ClassVar, Self

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, JsonShapeFamily, JsonShapeMember


@dataclass(frozen=True)
class Delivery(DeclaredFamily):
    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"{type(self).__name__} requires nonempty {item.name}.")

    @property
    @abstractmethod
    def summary(self) -> str: ...

    def require_completion(self) -> None:
        raise ValueError("A commanded goal is complete only with WorksInApp evidence.")

    def block_reason(self, next_step: str) -> str:
        return next_step


@dataclass(frozen=True)
class NotMerged(Delivery):
    @property
    def summary(self) -> str:
        return "Not merged"


@dataclass(frozen=True)
class Merged(Delivery):
    sha: str

    @property
    def summary(self) -> str:
        return f"Merged {self.sha}"


@dataclass(frozen=True)
class Installed(Delivery):
    runtime: str

    @property
    def summary(self) -> str:
        return f"Installed {self.runtime}"


@dataclass(frozen=True)
class WorksInApp(Delivery):
    evidence: str
    def require_completion(self) -> None:
        pass

    @property
    def summary(self) -> str:
        return f"Works in app: {self.evidence}"


@dataclass(frozen=True)
class Broken(Delivery):
    reason: str

    @property
    def summary(self) -> str:
        return f"Broken: {self.reason}"

    def block_reason(self, next_step: str) -> str:
        return self.reason


@dataclass(frozen=True)
class GoalReport:
    stupid: str
    deleted: str
    delivery: Delivery
    next: str

    def __post_init__(self) -> None:
        if not all(type(text) is str and text.strip()
                   for text in (self.stupid, self.deleted, self.next)):
            raise ValueError("Goal reports require nonempty stupid, deleted and next.")
        if not isinstance(self.delivery, Delivery):
            raise TypeError("Goal reports require a declared Delivery.")

    @property
    def summary(self) -> str:
        return (f"Stupid: {self.stupid}\nDeleted: {self.deleted}\n"
                f"Delivery: {self.delivery.summary}\nNext: {self.next}")


class GoalProgress(DeclaredFamily, JsonShapeFamily, affix="GoalProgress"):
    """Plain self-directed progress or a complete report; never both stored."""

    @classmethod
    def capture(cls, value: GoalProgress | GoalReport | str) -> GoalProgress:
        if isinstance(value, cls):
            return value
        return cls.from_wire(FieldCodec.encode(value))

    @property
    @abstractmethod
    def summary(self) -> str: ...

    @property
    @abstractmethod
    def block_reason(self) -> str: ...

    @property
    def deliveries(self) -> tuple[Delivery, ...]:
        return ()

    def require_report(self) -> GoalReport:
        raise ValueError("A commanded goal requires stupid, deleted, delivery and next.")

    def noted(self, note: str) -> Self:
        # Runtime notes must not replace facts in an agent's last report.
        return self

    def failed(self, diagnostic: str) -> GoalProgress:
        return self.noted(diagnostic)

    def __str__(self) -> str:
        return self.summary

    def __bool__(self) -> bool:
        return bool(self.summary)


@dataclass(frozen=True)
class TextGoalProgress(GoalProgress, JsonShapeMember):
    value: str = ""

    @property
    def summary(self) -> str:
        return self.value

    @property
    def block_reason(self) -> str:
        return self.value

    def noted(self, note: str) -> TextGoalProgress:
        return type(self)(f"{self.value}\n\n{note}" if self.value else note)

    def failed(self, diagnostic: str) -> TextGoalProgress:
        return type(self)(diagnostic)


@dataclass(frozen=True)
class ReportedGoalProgress(GoalProgress, JsonShapeMember):
    value: GoalReport

    @property
    def deliveries(self) -> tuple[Delivery, ...]:
        return (self.value.delivery,)

    def require_report(self) -> GoalReport:
        return self.value

    @property
    def summary(self) -> str:
        return self.value.summary

    @property
    def block_reason(self) -> str:
        return self.value.delivery.block_reason(self.value.next)
