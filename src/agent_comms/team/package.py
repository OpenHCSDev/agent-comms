"""Files shipped with the teammate, independent of any machine's configuration."""

from importlib.resources import files
from pathlib import Path
from typing import ClassVar


class TeamPackage:
    """The installed package owns its instructions and their declared locations."""

    @classmethod
    def rules(cls) -> Path:
        return Path(str(files(__package__) / "rules"))

    reading_order: ClassVar[tuple[str, ...]] = (
        "spine",
        "after-compaction",
        "unnecessary",
        "correct-maintenance",
        "slop-and-polymorphism",
        "owner-authority",
        "evidence-not-claims",
        "plain-reporting",
        "refactor-game",
        "no-blocking",
        "finish-line",
        "fork-orders",
        "tests-and-commands",
        "ship-and-install",
        "hotfix",
        "storage-and-cleanup",
        "send-backs-and-strikes",
        "blunt-reviews",
        "coordination",
        "self-correction",
        "deadlines",
    )
    """The one declaration of the order every deal and unit prompt presents the rules in. The
    papers are dealt before all of them; the spine comes next and kills the hedge on what they
    proved, then the price of calling an abstraction unnecessary; each rule after it is set up by
    the one before: the verdict index, the slop it defines, the owner whose positions they are,
    evidence, reporting, the work, then how the teammate enforces it. A rule this tuple does not name still loads, after the named ones in name order; the package test
    holds the tuple to exactly the packaged rules."""

    @classmethod
    def rules_files(cls) -> tuple[Path, ...]:
        """All package rule files in the reading order, shared by review, every deal and the
        Claude hook (.claude/hooks/rule_gate.py), which imports this dependency-free module."""
        rules = tuple(sorted(
            (path for path in cls.rules().iterdir() if path.is_file()),
            key=lambda path: (cls.reading_order.index(path.stem)
                              if path.stem in cls.reading_order else len(cls.reading_order),
                              path.name),
        ))
        if not rules:
            raise ValueError("The packaged teammate rules directory is empty.")
        return rules

    papers: ClassVar[tuple[str, ...]] = ("semantic-completeness", "less-is-harder")
    """The dealt papers in reading order, by their directory under `paper/`: the hierarchy every
    rule is a consequence of, then the price of proving anything unnecessary."""

    @classmethod
    def paper(cls) -> tuple[Path, ...]:
        """Every dealt paper's parts in reading order, each part one default read. Figures sit in
        each paper's `figures/`; paper1's supplement in `semantic-completeness/supplement` is
        optional and never dealt."""
        root = Path(str(files(__package__) / "paper"))
        return tuple(part for name in cls.papers for part in sorted((root / name).glob("*.md")))

    @classmethod
    def skill(cls) -> Path:
        return Path(str(files("agent_comms.skills") / "comms-team/SKILL.md"))

    @classmethod
    def patterns(cls) -> Path:
        return Path(str(files(__package__) / "patterns.md"))

    @classmethod
    def voice(cls) -> Path:
        return Path(str(files(__package__) / "voice.md"))
