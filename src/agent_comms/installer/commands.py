"""Process execution, diagnostics and timing for one installation."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.backup import UpgradeRefused
from agent_comms.installer.interpreter_operation import ProbeProgram, RuntimeProbe, SourceProgram


@dataclass(frozen=True)
class TimedCommand:
    """One command an install ran and how long it took."""

    command: str
    seconds: float

    def line(self) -> str:
        return f"{self.seconds:.1f}s {self.command}"


@dataclass(frozen=True)
class InstallCommands:
    """The commands one install runs. Its stacks and steps run every command through it,
    and the install's log and result both read what it ran."""

    ran: list[TimedCommand] = field(default_factory=list)

    @contextmanager
    def timing(self, command: str) -> Iterator[None]:
        print("+", command, file=sys.stderr, flush=True)
        started = time.monotonic()
        try:
            yield
        finally:
            timed = TimedCommand(command, round(time.monotonic() - started, 1))
            self.ran.append(timed)
            print("=", timed.line(), file=sys.stderr, flush=True)

    def run(self, *command: str | Path, cwd: Path = Path(),
            env: Mapping[str, str] = os.environ, label: str = "") -> str:
        with self.timing(label or " ".join(map(str, command))):
            completed = subprocess.run([str(part) for part in command], cwd=cwd, env=env,
                                       capture_output=True, text=True)
        if completed.returncode:
            raise UpgradeRefused(f"{command[0]} failed: {completed.stdout.strip()}\n"
                                 f"{completed.stderr.strip()}")
        return completed.stdout

    @contextmanager
    def exchange(self, encoded: str) -> Iterator[tuple[Path, Path]]:
        """Keep one request and its answer alive until the caller reads the result."""
        with tempfile.TemporaryDirectory() as scratch:
            request = Path(scratch) / "request.json"
            request.write_text(encoded)
            yield request, Path(scratch) / "result.json"

    def probe[Answer](self, probe: RuntimeProbe[Answer], python: Path, *,
                      cwd: Path = Path(), env: Mapping[str, str] = os.environ,
                      program: ProbeProgram = SourceProgram()) -> Answer:
        with self.exchange(FieldCodec.compact(FieldCodec.encode(probe))) as (request, result):
            self.run(*probe.command(python, request, result, program=program), cwd=cwd, env=env,
                     label=f"{python} {type(probe).__name__}")
            return probe.decode_answer(result.read_text())

