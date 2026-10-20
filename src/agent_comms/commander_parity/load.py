"""Observe the native load measurement used by the installed capacity command."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..commander.config import CommanderConfig
from ..field_codec import FieldCodec
from ..private_path import FileRevision
from . import ParityComparison, UnverifiedParity

# Profile the real return, never replace the measurement, command, bus or reply.
# Comparing independent reads of a running fleet confuses new edits with drift.
OBSERVE_CAPACITY = """
import contextlib, io, json, sys
import agent_comms.commander.capacity as capacity
from agent_comms.commander.cli import main
measured = []
contexts = []
def observe(frame, event, value):
    if event == 'return' and frame.f_code is capacity.Load.of.__func__.__code__:
        measured.append(value)
    if event == 'call' and frame.f_code is capacity.main.__code__:
        contexts.append(frame.f_locals['ctx'])
output = io.StringIO()
sys.setprofile(observe)
try:
    with contextlib.redirect_stdout(output):
        main()
finally:
    sys.setprofile(None)
if len(measured) != 1 or len(contexts) != 1:
    raise ValueError('Capacity must use exactly one native measurement and selected context')
config = contexts[0]
print(json.dumps(dict(current=output.getvalue(), native=str(measured[0]),
                      source=capacity.__file__, root=str(config.root),
                      commander=config.commander)))
"""


def state_files(directory: Path) -> tuple[tuple[str, FileRevision], ...]:
    return tuple(
        (str(path.relative_to(directory)), FileRevision.from_stat(path.stat()))
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    )


@dataclass(frozen=True)
class MeasuredOutput:
    current: str
    native: str
    source: str
    root: str
    commander: str


@dataclass(frozen=True)
class LoadComparison(ParityComparison):
    observation: MeasuredOutput
    state_before: tuple[tuple[str, FileRevision], ...]
    state_after: tuple[tuple[str, FileRevision], ...]

    @property
    def passed(self) -> bool:
        return (
            self.observation.current.strip() == self.observation.native
            and self.state_before == self.state_after
        )

    def describe(self) -> str:
        return (
            f"current={self.observation.current.strip()!r}; "
            f"native={self.observation.native!r}; owner={self.observation.source}; "
            f"commander={self.observation.commander}; root={self.observation.root}; "
            f"private_state_unchanged={self.state_before == self.state_after}"
        )

    @classmethod
    def read(cls) -> ParityComparison:
        config = CommanderConfig.read(CommanderConfig.path())
        before = state_files(config.state)
        # -I uses installed imports even when this candidate runs with PYTHONPATH=src.
        # HOME and the environment stay unchanged; the actual CLI main runs.
        current = subprocess.run(
            (str(config.cli.parent / "python"), "-I", "-c", OBSERVE_CAPACITY, "capacity"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if current.returncode:
            return UnverifiedParity(current.stderr)
        observed = FieldCodec.decode(MeasuredOutput, FieldCodec.parse(current.stdout))
        source = Path(observed.source)
        if (
            observed.root != str(config.root)
            or observed.commander != config.commander
            or not any(source.is_relative_to(repo.checkout) for repo in config.repos)
        ):
            return UnverifiedParity(
                "The installed command used a different configuration or source"
            )
        return cls(observed, before, state_files(config.state))
