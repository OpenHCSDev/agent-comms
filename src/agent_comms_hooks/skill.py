"""At session start, resume and compaction, write the commander's sources and order them read.

The sources are the commander's voice, the indictment, the recorded failures, the commander skill,
the packaged rules and the refactor-audit catalog's index; a rung file is read when an order or
roast cites its pattern ID. Claude Code caps
injected hook text at 10,000 characters and the sources are far longer, so they
go into parts beside the transcript and the hook injects the order to read
each part. The reread hook repeats that order as context grows.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import ValidationError

from agent_comms.behaviour_patterns import PostmortemFailure
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.rule_file import RuleFile
from agent_comms.todos import Slop
from agent_comms_hooks import ClaudeHookProgram
from agent_comms_hooks.reread import ContextCadence, SourcesOrder, ToolUseReminder


@dataclass(frozen=True)
class CommanderStart(SourcesOrder):
    part_chars: ClassVar[int] = 60_000
    """The most characters one Read of a part returns in full."""

    def due(self, tokens: int) -> bool:
        return True

    def prepare(self) -> None:
        """A start or compaction drops what was read, so every reminder is due again."""
        for member in ToolUseReminder.__subclasses__():
            Path(self.transcript_path).with_suffix(member.mark_suffix).unlink(missing_ok=True)
        self.voice.write_text(CommanderConfig.packaged_voice().read_text(encoding="utf-8"), encoding="utf-8")
        self.drift.write_text("\n".join(RuleFile.drift_tells()), encoding="utf-8")
        self.parts.mkdir(exist_ok=True)
        for old in self.parts.iterdir():
            old.unlink()
        chunks: list[list[str]] = [[]]
        size = 0
        for line in self.sources().splitlines(keepends=True):
            if size + len(line) > self.part_chars and chunks[-1]:
                chunks.append([])
                size = 0
            chunks[-1].append(line)
            size += len(line)
        for number, chunk in enumerate(chunks, 1):
            (self.parts / f"{number:02}.md").write_text("".join(chunk), encoding="utf-8")

    @classmethod
    def sources(cls) -> str:
        return "\n\n".join(
            (
                cls.notice(),
                CommanderConfig.packaged_voice().read_text(encoding="utf-8", newline=""),
                CommanderConfig.packaged_indictment().read_text(encoding="utf-8", newline=""),
                "# Recorded failures\n\n" + PostmortemFailure.catalog(),
                RuleFile.document(CommanderConfig.packaged_skill())[1],
                *(rule.render() for rule in RuleFile.packaged()),
                (Slop.catalog() / "README.md").read_text(encoding="utf-8", newline=""),
            )
        )

    @staticmethod
    def notice() -> str:
        try:
            return f"Commander: {CommanderConfig.read(CommanderConfig.path()).commander}."
        except (
            FileNotFoundError,
            PermissionError,
            IsADirectoryError,
            UnicodeError,
            ValidationError,
        ):
            return "No commander is configured (installed configuration is missing or invalid)."


class SkillText(ClaudeHookProgram):
    events = ("SessionStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    ContextCadence.remind(CommanderStart)


if __name__ == "__main__":
    main()
