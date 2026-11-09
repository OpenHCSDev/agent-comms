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

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.projects.change_report import CitationProject, FileLine
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.slop import Slop
from agent_comms.team.package import TeamPackage


@dataclass(frozen=True)
class ProbeContext(CitationProject):
    """What a probe answer is checked against: never the rule text it could copy. It is the
    citation project the session cites in, which is the team's registered repositories as well
    as the directory it was launched in: a session launched outside a repository of ours (a
    course project, a notes directory) still works in the team's checkouts, and on 2026-11-07
    one launched in /home/ts/code/projects/bmde-520-001 could not answer the hunt probe at all,
    because every line it was about to edit lives in a Toad worktree and the only tools that
    could have found a line in the launch directory were the ones this gate holds."""

    owner_words: tuple[str, ...]
    """Tristan's own prompts as the deal recorded them."""
    worktree: Path
    """The directory the session was launched in; an unqualified citation is read here first."""
    repos: tuple[Repo, ...] = ()
    """The team's registered repositories, as the deal recorded them: a cited line may name one.
    Empty when the bus registers no project, which leaves the launch directory alone."""
    copied_from: tuple[str, ...] = ()
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
        return tuple(repository.github for repository in Repo.candidates(self.repos, name))

    def registered_repos(self) -> tuple[Repo, ...]:
        return self.repos

    def roots(self) -> tuple[Path, ...]:
        """Where an unqualified file:line is read: the launch directory, then every registered
        checkout. A session's work is wherever the team's repositories are checked out, which
        is rarely where it was started."""
        return (self.worktree, *(repository.checkout for repository in self.repos))

    def line_exists(self, line: FileLine) -> bool:
        return any(self.line_in(root, line) for root in self.roots())

    def line_in(self, root: Path, line: FileLine) -> bool:
        path = root / line.file
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
    question = ("Name one claim in this session's work that a dealt result corrects. Copy the"
                " recorded prompt line that corrected it, and name the result.")

    words: str
    """The correcting line, copied exactly from one of the recorded prompts."""
    prior: str
    """The corrected claim, in one plain sentence about the work."""
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
    question = ("One restated fact in the code you are about to edit, as its formal statement"
                " (pattern, fact, at, derive, by), with at and by as path:line in a checkout.")

    def fault(self, context: ProbeContext) -> str:
        missing = [line.to_wire() for line in (self.at, *self.by.cited())
                   if not context.line_exists(line)]
        if missing:
            # Name every checkout that was read: a session cannot discover them from here,
            # because this gate holds the tools that would list them.
            return (f"{', '.join(missing)} is not a line in "
                    + " or ".join(str(root) for root in context.roots()))
        return context.copied(self.fact)

    def text(self) -> str:
        return self.fact


@dataclass(frozen=True)
class FlinchProbe(Probe):
    question = ("Did a check change one of your plans this session? Yes or no, then what the"
                " check showed, citing a file:line or a 12-hex message id.")

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
