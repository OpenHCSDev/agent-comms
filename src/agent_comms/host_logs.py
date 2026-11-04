"""What a teammate on another machine asks the host for: its logs and busiest processes, read-only
and with secrets hidden, so a cloud session can read a failure itself."""

from __future__ import annotations

import re
import subprocess
from abc import abstractmethod
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .nominal.declared_family import DeclaredFamily
from .tool_result import ToolResult


class HostLog(DeclaredFamily, affix="HostLog"):
    """One thing the host can show: its members say where it reads, and every read is bounded."""

    timeout_seconds: ClassVar[int] = 10
    secrets: ClassVar[tuple[tuple[re.Pattern[str], str], ...]] = (
        (re.compile(r'(?:wss?://[^\s"\'\\]+)?/watch/[\w-]+'), "[inbox watch URL]"),
        (re.compile(r"(?i)\b(bearer)\s+[\w.~+/=-]+"), r"\1 [hidden]"),
        (re.compile(r"(?i)\b(token|secret|password|authorization|code)(['\"]?\s*[=:]\s*['\"]?)[\w.~+/=-]{8,}"),
         r"\1\2[hidden]"),
        (re.compile(r'"token":"[^"]+"'), '"token":"[hidden]"'),
    )

    @classmethod
    @abstractmethod
    def lines(cls, root: Path, count: int) -> tuple[str, ...]: ...

    @classmethod
    def run(cls, *command: str) -> tuple[str, ...]:
        done = subprocess.run(command, capture_output=True, text=True, errors="replace",
                              timeout=cls.timeout_seconds, check=False)
        return tuple((done.stdout + done.stderr).splitlines())

    @classmethod
    def hidden(cls, line: str) -> str:
        for pattern, replacement in cls.secrets:
            line = pattern.sub(replacement, line)
        return line

    @classmethod
    def tail(cls, path: Path, count: int) -> tuple[str, ...]:
        with path.open(errors="replace") as stream:
            return tuple(line.rstrip("\n") for line in deque(stream, count))

    @classmethod
    def newest(cls, directory: Path, pattern: str, count: int) -> tuple[str, ...]:
        """The tail of the most recently written match, named first; none is an answer too."""
        found = max(directory.glob(pattern), key=lambda path: path.stat().st_mtime, default=None)
        return () if found is None else (str(found), *cls.tail(found, count))


class ConnectorHostLog(HostLog):
    """The connector service's journal, where a failed tool call leaves its traceback."""

    @classmethod
    def lines(cls, root: Path, count: int) -> tuple[str, ...]:
        return cls.run("journalctl", "--user", "--unit", "agent-comms-connector.service",
                       "--lines", str(count), "--no-pager", "--output", "short-iso")


class InstallHostLog(HostLog):
    """The latest install's own diagnostics, beside the root it installed."""

    @classmethod
    def lines(cls, root: Path, count: int) -> tuple[str, ...]:
        return cls.newest(root.parent, f".{root.name}.installer-*/stderr.log", count)


class ToadHostLog(HostLog):
    """The newest failure Toad wrote: one file per failure."""

    @classmethod
    def lines(cls, root: Path, count: int) -> tuple[str, ...]:
        return cls.newest(Path.home() / ".local/state/toad/logs", "*.txt", count)


class ProcessesHostLog(HostLog):
    """The busiest processes now, by lifetime CPU share, with resident memory in KiB. Every
    process on the desktop is listed, so an argument list is cut to what names the program:
    the rest of a browser's or anyone else's command line is not a teammate's to read."""

    shown_columns: ClassVar[int] = 150

    @classmethod
    def lines(cls, root: Path, count: int) -> tuple[str, ...]:
        listed = cls.run("ps", "-eo", "pid,ppid,etime,pcpu,rss,args", "--sort=-pcpu")[:count]
        return tuple(line[:cls.shown_columns] for line in listed)


@dataclass(frozen=True)
class HostLogRead(ToolResult):
    """Lines as the host holds them, with secrets hidden."""

    lines: tuple[str, ...]
