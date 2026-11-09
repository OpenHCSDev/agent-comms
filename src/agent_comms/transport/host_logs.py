"""What a teammate on another machine asks the host for: its logs and busiest processes, read-only
and with secrets hidden, so a cloud session can read a failure itself.

Only what names no user content is offered: no denylist can certify free text clean, so the values
this process holds are hidden by value first and the patterns are the backstop for the rest."""

from __future__ import annotations

import os
import re
import subprocess
from abc import abstractmethod
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.transport.tool_result import ToolResult


class HostLog(DeclaredFamily, affix="HostLog"):
    """One thing the host can show: its members say where it reads, and every read is bounded."""

    timeout_seconds: ClassVar[int] = 10
    secret_name: ClassVar[re.Pattern[str]] = re.compile(r"(?i)KEY|TOKEN|SECRET|PASS|CREDENTIAL|COOKIE|AUTH")
    shortest_secret: ClassVar[int] = 6
    secrets: ClassVar[tuple[tuple[re.Pattern[str], str], ...]] = (
        (re.compile(r'(?:wss?://[^\s"\'\\]+)?/watch/[\w-]+'), "[inbox watch URL]"),
        (re.compile(r"://[^\s/@]+@"), "://[hidden]@"),
        (re.compile(r"(?i)\b(bearer)\s+[\w.~+/=%-]+"), r"\1 [hidden]"),
        (re.compile(r"(?i)\b(basic)\s+[\w.~+/=%-]{8,}"), r"\1 [hidden]"),
        (re.compile(r"\b(?:gh[pousr]_|github_pat_|sk-|xox[abprs]-|glpat-|AKIA)[\w-]{8,}"), "[hidden]"),
        (re.compile(r"\beyJ[\w-]+\.[\w-]+(?:\.[\w-]+)?"), "[hidden]"),
        (re.compile(r"\bAIza[\w-]{30,}"), "[hidden]"),
        (re.compile(r"(\s-p)\S{3,}"), r"\1[hidden]"),
        (re.compile(r"(?i)([\w-]*(?:token|secret|pass(?:word|wd)?|credentials?|cookie|auth(?:orization)?|[_-]key)"
                    r"['\"]?\s*[=:]\s*['\"]?)(?!bearer\b|basic\b)[\w.~+/=%-]+"), r"\1[hidden]"),
        (re.compile(r"(?i)([\w-]*(?:key|code|session)['\"]?\s*[=:]\s*['\"]?)[\w.~+/=%-]{8,}"), r"\1[hidden]"),
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
    def read(cls, root: Path, count: int) -> tuple[str, ...]:
        held = cls.held_values(os.environ)
        return tuple(cls.hidden(line, held) for line in cls.lines(root, count))

    @classmethod
    def held_values(cls, environment: Mapping[str, str]) -> tuple[str, ...]:
        """Longest first, so a value that contains another is hidden whole."""
        return tuple(sorted(
            {value for name, value in environment.items()
             if cls.secret_name.search(name) and len(value) >= cls.shortest_secret},
            key=len, reverse=True))

    @classmethod
    def hidden(cls, line: str, held: tuple[str, ...] = ()) -> str:
        for value in held:
            line = line.replace(value, "[hidden]")
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


class ProcessesHostLog(HostLog):
    """The busiest processes now, by lifetime CPU share, with resident memory in KiB. Every
    process on the desktop is listed, so each is named by its program alone: anyone's command
    line is not a teammate's to read."""

    @classmethod
    def lines(cls, root: Path, count: int) -> tuple[str, ...]:
        return cls.run("ps", "-eo", "pid,ppid,etime,pcpu,rss,comm", "--sort=-pcpu")[:count]


@dataclass(frozen=True)
class HostLogRead(ToolResult):
    """Lines as the host holds them, with secrets hidden."""

    lines: tuple[str, ...]
