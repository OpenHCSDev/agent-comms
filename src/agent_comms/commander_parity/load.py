"""Observe the native load measurement used by the installed capacity command."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..child_process import BoundedRun
from ..commander.config import CommanderConfig
from ..field_codec import FieldCodec
from ..private_path import FileRevision
from . import ParityComparison, UnverifiedParity
from .observations import CapacityObservation, MeasuredOutput


def state_files(directory: Path) -> tuple[tuple[str, FileRevision], ...]:
    return tuple(
        (str(path.relative_to(directory)), FileRevision.from_stat(path.stat()))
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    )


@dataclass(frozen=True)
class CommandComparison(ParityComparison):
    arguments: ClassVar[tuple[str, ...]]
    timeout: ClassVar[int] = 30
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
        return cls.invoke(CommanderConfig.read(CommanderConfig.path()), cls.arguments)

    @classmethod
    def invoke(cls, config: CommanderConfig, arguments: tuple[str, ...]) -> ParityComparison:
        """Bind one native invocation and all its observations to the selected configuration."""
        before = state_files(config.state)
        # -I uses installed imports even when this candidate runs with PYTHONPATH=src.
        # HOME and the environment stay unchanged; the actual CLI main runs.
        current = asyncio.run(
            BoundedRun.run(
                (str(config.cli.parent / "python"), "-I", *arguments),
                timeout=cls.timeout,
            )
        )
        if not current.outcome.successful:
            return UnverifiedParity(
                f"{cls.__name__}: configured native owner {current.outcome.summary}; "
                f"bound={cls.timeout}s; {current.stderr.decode(errors='replace')}"
            )
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


class LoadComparison(CommandComparison):
    arguments = CapacityObservation.arguments()
