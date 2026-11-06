"""The questions a deal asks after the reading: each costs a look at the code or at Tristan's
own words, so a template cannot answer it.

On 2026-11-06 the per-rule audit (quote, does, why for every rule) was filled by template from
about its twelfth rule on: true, and changing nothing. Tristan: "the probes need to be costly
too ... force answers that break the patterns and show self-awareness". Each probe here can only
be answered by reading something the rules do not contain: his recorded words, the paper's
numbered results, the code about to be touched, the session's own resistance."""

from __future__ import annotations

import re
from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import ClassVar

from agent_comms.change_report import CitationProject, FileLine
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.repo import Repo
from agent_comms.repository_name import RepositoryName
from agent_comms.slop import Slop
from agent_comms.team.package import TeamPackage


@dataclass(frozen=True)
class ProbeContext(CitationProject):
    """What a probe answer is checked against: never the rule text it could copy. It is the
    citation project of the session's own checkout: a cited file:line names a line there, and
    no other repository is registered for a prefix to name."""

    owner_words: tuple[str, ...]
    """Tristan's own prompts as the deal recorded them."""
    worktree: Path
    """The checkout the session works in; cited lines must exist there."""
    copied_from: tuple[str, ...]
    """Text an answer must not mostly repeat: every dealt rule and every other answer."""

    shingle: ClassVar[int] = 5
    copied_share: ClassVar[float] = 0.5

    RESULT: ClassVar[re.Pattern[str]] = re.compile(
        r"\b(Theorem|Proposition|Corollary|Lemma)\s+(\d+\.\d+)\b")

    @cached_property
    def results(self) -> frozenset[tuple[str, str]]:
        """Every numbered result the dealt papers state, as (kind, number)."""
        return frozenset(
            (kind, number) for paper in TeamPackage.paper()
            for kind, number in self.RESULT.findall(paper.read_text(encoding="utf-8")))

    @staticmethod
    def shingles(text: str, size: int) -> frozenset[tuple[str, ...]]:
        words = re.findall(r"[a-z0-9']+", text.lower())
        return frozenset(tuple(words[index:index + size]) for index in range(len(words) - size + 1))

    def copied(self, answer: str) -> str:
        """Why `answer` is mostly someone else's words, or nothing when it is its own."""
        own = self.shingles(answer, self.shingle)
        if not own:
            return ""
        seen = frozenset().union(*(self.shingles(text, self.shingle) for text in self.copied_from))
        share = len(own & seen) / len(own)
        if share >= self.copied_share:
            return (f"{share:.0%} of it repeats the rules or another answer; say what you"
                    " actually see, in your own words")
        return ""

    def repository_names(self, name: str = "") -> tuple[RepositoryName, ...]:
        return ()

    def registered_repos(self) -> tuple[Repo, ...]:
        return ()

    def line_exists(self, line: FileLine) -> bool:
        path = self.worktree / line.file
        if not path.is_file():
            return False
        return 0 < line.line <= len(path.read_text(encoding="utf-8").splitlines())


class Probe(DeclaredFamily, affix="Probe"):
    """One costly question: each member declares what it asks and how its answer is checked."""

    family_discriminator: ClassVar[str] = "probe"
    question: ClassVar[str]

    @abstractmethod
    def fault(self, context: ProbeContext) -> str:
        """Why this answer does not answer its question, or nothing when it does."""

    @abstractmethod
    def text(self) -> str:
        """The answer's own words, which the no-reuse check reads."""

    @classmethod
    def applies(cls, owner_words: tuple[str, ...]) -> bool:
        """Whether a deal with these recorded prompts can ask this probe."""
        return True

    @classmethod
    def owed(cls, owner_words: tuple[str, ...]) -> tuple[type[Probe], ...]:
        """Every probe this deal asks, one answer each."""
        return tuple(
            probe for probe in cls.members_with(Probe) if probe.applies(owner_words))

    @classmethod
    def missing(
        cls, answers: Iterable[Probe], owner_words: tuple[str, ...],
    ) -> tuple[type[Probe], ...]:
        given = {type(answer) for answer in answers}
        return tuple(probe for probe in cls.owed(owner_words) if probe not in given)


@dataclass(frozen=True)
class PriorProbe(Probe):
    question = ("Name a prior you brought into this session that the papers refute. Quote"
                " Tristan's own words where he corrected it, and the result that refutes it.")

    words: str
    """Tristan's words, copied exactly from one of his recorded prompts."""
    prior: str
    """The prior, in your words."""
    refuted_by: str
    """The paper's result, e.g. "Corollary 4.12"."""

    @classmethod
    def applies(cls, owner_words: tuple[str, ...]) -> bool:
        """With no recorded words of his there is nothing to quote."""
        return bool(owner_words)

    def fault(self, context: ProbeContext) -> str:
        if not self.words.strip() or not any(self.words in said for said in context.owner_words):
            return "its words are not copied exactly from one of Tristan's recorded prompts"
        if not set(ProbeContext.RESULT.findall(self.refuted_by)) & context.results:
            return ("its refuted_by names no numbered result the dealt papers state"
                    " (e.g. \"Corollary 4.12\")")
        return context.copied(self.prior)

    def text(self) -> str:
        return self.prior


@dataclass(frozen=True)
class HuntProbe(Slop, Probe):
    question = ("Before you touch anything: one restated fact in the code you are about to edit,"
                " as its formal statement (pattern, fact, at, derive, by).")

    def fault(self, context: ProbeContext) -> str:
        missing = [line.to_wire() for line in (self.at, *self.by.cited())
                   if not context.line_exists(line)]
        if missing:
            return f"{', '.join(missing)} is not a line in {context.worktree}"
        return context.copied(self.fact)

    def text(self) -> str:
        return self.fact


@dataclass(frozen=True)
class FlinchProbe(Probe):
    question = ("Where did you resist Tristan this session? Yes or no, then the hypothesis you had,"
                " or what the code showed once you checked, citing a file:line or a message id.")

    answer: str

    EVIDENCE: ClassVar[re.Pattern[str]] = re.compile(r"\b[0-9a-f]{12}\b")
    OPENS: ClassVar[re.Pattern[str]] = re.compile(r"^[\W_]*(yes|no)\b", re.I)

    def fault(self, context: ProbeContext) -> str:
        if not self.OPENS.match(self.answer):
            return "it does not open with yes or no"
        cited = FileLine.scan(self.answer, context)
        if not any(context.line_exists(line) for line in cited) and not self.EVIDENCE.search(
                self.answer):
            return "it cites no file:line in this checkout and no message id"
        return context.copied(self.answer)

    def text(self) -> str:
        return self.answer
