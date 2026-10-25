"""Put Tristan's scoreboard in front of the commander where it cannot be filed as background.

The heartbeat rewrites each commander's HUD frame every beat; its owed section is Tristan's
scoreboard. The session start hook writes beside the transcript where this session's frame
lives. Every owner prompt carries the frame ahead of his words, and a turn cannot end while
the frame owes anything or is too old to trust. Nothing reaches the commander through the
Monitor or a task notification, which the model files as ambient noise. The hook imports only
the standard library and reads the frame file, so it costs a file read, not a Core start.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms_hooks import ClaudeHookProgram, ClaudeTranscriptHook


class Board(ABC):
    """What one read of the frame files shows the commander."""

    @abstractmethod
    def text(self) -> str: ...

    @abstractmethod
    def ends_turn(self) -> bool:
        """Whether the commander may end its turn on this board."""


@dataclass(frozen=True)
class FrameBoard(Board):
    """The frames as the heartbeat wrote them: their lines and how many lines they owe."""

    lines: tuple[str, ...]
    owed: int

    def text(self) -> str:
        return "\n".join(self.lines)

    def ends_turn(self) -> bool:
        return not self.owed


@dataclass(frozen=True)
class DeadBoard(Board):
    """No frame to trust: the heartbeat is down, or this session was never bound to one."""

    failure: str

    def text(self) -> str:
        return (f"SCOREBOARD DEAD: {self.failure}. Every owed line is invisible until the"
                " heartbeat writes this commander's frame again; restoring it comes first.")

    def ends_turn(self) -> bool:
        return False


@dataclass(frozen=True)
class ScoreboardHook(ClaudeTranscriptHook):
    stale: ClassVar[float] = 10 * 60
    """A frame older than this means the heartbeat stopped beating."""

    @property
    def pointer(self) -> Path:
        """The frame files of this session's commanders, one path per line."""
        return Path(self.transcript_path).with_suffix(".frame")

    def board(self) -> Board:
        if not self.pointer.exists():
            return DeadBoard(f"no frame pointer at {self.pointer}")
        paths = tuple(Path(line) for line in self.pointer.read_text(encoding="utf-8").split())
        if not paths:
            return DeadBoard("this session is bound to no commander")
        missing = tuple(str(path) for path in paths if not path.exists())
        if missing:
            return DeadBoard(f"no frame at {', '.join(missing)}")
        old = tuple(path for path in paths if path.stat().st_mtime < time.time() - self.stale)
        if old:
            minutes = int((time.time() - min(path.stat().st_mtime for path in old)) // 60)
            return DeadBoard(f"the newest frame is {minutes} min old")
        frames = tuple(json.loads(path.read_text(encoding="utf-8")) for path in paths)
        return FrameBoard(tuple(line for frame in frames for line in frame["frame"]),
                          sum(frame["owed_count"] for frame in frames))


@dataclass(frozen=True)
class StopScoreboard(ScoreboardHook):
    def guard(self) -> None:
        """A turn ends only on a board that owes nothing, however often it was sent back."""
        if not self.parts.exists():
            return
        board = self.board()
        if not board.ends_turn():
            print(json.dumps({"decision": "block",
                              "reason": f"Act on every owed line before ending the turn.\n"
                                        f"{board.text()}"}))


@dataclass(frozen=True)
class PromptScoreboard(ScoreboardHook):
    def show(self) -> None:
        if self.parts.exists():
            self.warn(f"{self.board().text()}\nAnswer Tristan below, then work this board.")


class ScoreboardOnStop(ClaudeHookProgram):
    events = ("Stop",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{stop.__module__}:{stop.__name__}")


class ScoreboardOnPrompt(ClaudeHookProgram):
    events = ("UserPromptSubmit",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{prompt.__module__}:{prompt.__name__}")


def stop() -> None:
    StopScoreboard.read().guard()


def prompt() -> None:
    PromptScoreboard.read().show()
