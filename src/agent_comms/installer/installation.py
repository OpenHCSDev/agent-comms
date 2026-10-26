"""One install request: the checkout, target and mode an install runs, owned by the installer
and composed by the command line's install command."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.field_codec import AbsolutePathText, Argument, FieldCodec, Normalize, WireName
from agent_comms.install_choices import InstallerChoice, LiveMode, RehearsalMode, RunMode, UnnamedInstaller
from agent_comms.installer.probes.harnesses import HarnessInstallation


@dataclass(frozen=True, kw_only=True)
class Installation(HarnessInstallation):
    checkout: Annotated[
        Path,
        AbsolutePathText,
        Argument(flags=("--checkout",)),
    ] = dataclasses.field(default_factory=Path.cwd)
    to: Annotated[str, Argument(flags=("--to",))] = "origin/main"
    installer: Annotated[
        InstallerChoice,
        Argument(
            flags=("--installer",),
            options={"help": "The unit running this install; the next install's plan names it"},
        ),
    ] = UnnamedInstaller()
    mode: Annotated[
        RunMode,
        WireName("rehearse"),
        Normalize(bool),
        Argument(flags=("--rehearse",), options={
            "help": "Carry and read a copy of the bus; change nothing",
            "action": "store_const", "const": FieldCodec.encode(RehearsalMode()),
        }),
    ] = dataclasses.field(default_factory=LiveMode)
    retire: Annotated[
        bool,
        Argument(
            flags=("--retire",),
            options={"help": "Move the bus aside, kept, and install a fresh one in its place"},
        ),
    ] = False
    spill_dir: Annotated[
        Path,
        AbsolutePathText,
        Argument(
            flags=("--spill-dir",),
            options={"help": "Parent directory for live backups"},
        ),
    ] = dataclasses.field(default_factory=lambda: Path.home() / ".cache/agent-rehearsal")
