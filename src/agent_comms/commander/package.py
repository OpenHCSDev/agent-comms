"""Files shipped with the commander, independent of any machine's configuration."""

from importlib.resources import files
from pathlib import Path


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

    @classmethod
    def skill(cls) -> Path:
        return Path(str(files("agent_comms.skills") / "comms-commander/SKILL.md"))

    @classmethod
    def indictment(cls) -> Path:
        return Path(str(files(__package__) / "indictment.md"))

    @classmethod
    def voice(cls) -> Path:
        return Path(str(files(__package__) / "voice.md"))
