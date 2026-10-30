"""Files shipped with the commander, independent of any machine's configuration."""

from importlib.resources import files
from pathlib import Path
from typing import ClassVar


class CommanderPackage:
    """The installed package owns its instructions and their declared locations."""

    @classmethod
    def rules(cls) -> Path:
        return Path(str(files(__package__) / "rules"))

    @classmethod
    def rules_files(cls) -> tuple[Path, ...]:
        """All package rule files, shared by review and session context."""
        rules = tuple(sorted(path for path in cls.rules().iterdir() if path.is_file()))
        if not rules:
            raise ValueError("The packaged commander rules directory is empty.")
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
        return Path(str(files("agent_comms.skills") / "comms-commander/SKILL.md"))

    @classmethod
    def indictment(cls) -> Path:
        return Path(str(files(__package__) / "indictment.md"))

    @classmethod
    def voice(cls) -> Path:
        return Path(str(files(__package__) / "voice.md"))
