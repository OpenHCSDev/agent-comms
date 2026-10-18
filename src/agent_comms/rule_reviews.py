"""The commander's rule reviews: the rules each reviewer's latest review drew, and each answer."""

from __future__ import annotations

import re
import sqlite3
import time
from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .thread_owned_state import ThreadOwnedState
from .typed_table import Column, TypedStore, TypedTable

if TYPE_CHECKING:
    from .threads import Thread


@dataclass(frozen=True)
class DrawnRule(TypedTable):
    """One rule a reviewer's latest review drew."""

    reviewer: str = field(metadata={"sql": Column(primary_key=True)})
    n: int = field(metadata={"sql": Column(primary_key=True, check="n>0")})
    rule: str
    cited: bool
    drawn_at: float


@dataclass(frozen=True)
class RuleAnswer(TypedTable):
    """What a reviewer's tick does about drawn rule ``n``; without one the rule is unanswered."""

    reviewer: str = field(metadata={"sql": Column(primary_key=True)})
    n: int = field(metadata={"sql": Column(primary_key=True, check="n>0")})
    text: str


class RuleReviewEntry(DeclaredFamily, affix="RuleReviewEntry"):
    """One write to a reviewer's rule review."""

    @abstractmethod
    def write(self, db: sqlite3.Connection, reviewer: str) -> None: ...


@dataclass(frozen=True)
class DrawnRuleReviewEntry(RuleReviewEntry):
    """A new review replaces the reviewer's last; the first ``cited`` rules need a cited answer."""

    drawn: tuple[str, ...]
    cited: int

    def __post_init__(self) -> None:
        if not self.drawn or not 0 <= self.cited <= len(self.drawn):
            raise ValueError("A review draws at least one rule and cites at most every drawn rule.")

    def write(self, db: sqlite3.Connection, reviewer: str) -> None:
        DrawnRule.delete(db, reviewer=reviewer)
        RuleAnswer.delete(db, reviewer=reviewer)
        at = time.time()
        for n, rule in enumerate(self.drawn, 1):
            DrawnRule(reviewer, n, rule, n <= self.cited, at).insert(db)


@dataclass(frozen=True)
class AnsweredRuleReviewEntry(RuleReviewEntry):
    """What this tick does about drawn rule ``n``."""

    least_words: ClassVar[int] = 10
    n: int
    text: str

    def __post_init__(self) -> None:
        if len(self.text.split()) < self.least_words:
            raise ValueError(
                f"Say in {self.least_words}+ words what this tick does about the rule,"
                " naming a unit, problem or file."
            )

    def write(self, db: sqlite3.Connection, reviewer: str) -> None:
        drawn = DrawnRule.one(db, reviewer=reviewer, n=self.n)
        if drawn is None:
            raise ValueError(f"{reviewer} has no review that drew rule {self.n}.")
        if drawn.cited and not re.search(r"\S+\.py:\d+|P\d+", self.text):
            raise ValueError(
                "A correct-maintenance answer names the file:line or P-number it was applied to."
            )
        RuleAnswer(reviewer, self.n, self.text).upsert(db)


@dataclass(frozen=True)
class RuleReviews(ThreadOwnedState, TypedStore):
    root: Path
    filename = "rule_reviews.sqlite3"
    rows = (DrawnRule, RuleAnswer)

    def unanswered(self, reviewer: str) -> tuple[float, tuple[int, ...]] | None:
        """When the reviewer's latest review drew and which drawn rules lack an answer."""
        with self.snapshot() as db:
            drawn = DrawnRule.select(db, reviewer=reviewer, order_by=("n",))
            answered = {answer.n for answer in RuleAnswer.select(db, reviewer=reviewer)}
        if not drawn:
            return None
        return drawn[0].drawn_at, tuple(rule.n for rule in drawn if rule.n not in answered)

    def write(self, reviewer: str, entry: RuleReviewEntry) -> None:
        with self.writing() as db:
            entry.write(db, reviewer)

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        with self.writing() as db:
            for thread in threads:
                DrawnRule.delete(db, reviewer=thread.name)
                RuleAnswer.delete(db, reviewer=thread.name)
