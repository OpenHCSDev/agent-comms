"""The install operation's options, shared by CLI dispatch and teammate launches."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from agent_comms.installer.install_choices import (
    InstallerChoice,
    LiveMode,
    RehearsalMode,
    RunMode,
    UnnamedInstaller,
)
from agent_comms.installer.probes.harnesses import HarnessInstallation
from agent_comms.installer.probes.inputs import SelectedInputsProbe
from agent_comms.nominal.field_codec import (
    AbsolutePathText,
    Argument,
    FieldCodec,
    Normalize,
    NullMember,
    WireName,
)


@dataclass(frozen=True)
class RetiredCommanderChoice(NullMember):
    """What the runtime installed before the no-lead carry still sends for its removed
    `commander` option: always null, read by nothing. The old CLI starts the install that replaces
    it, so the new installer must decode its record. Delete once no installed runtime predates it.
    """


@dataclass(frozen=True, kw_only=True)
class InstallRequest(HarnessInstallation):
    kind: Literal["install"] = "install"
    previous_commander: Annotated[RetiredCommanderChoice, WireName("commander")] = (
        RetiredCommanderChoice()
    )
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
    selected_inputs: Annotated[
        tuple[SelectedInputsProbe, ...],
        Argument(
            flags=("--selected-inputs",),
            options={
                "type": FieldCodec.parse,
                "help": "JSON array of selected-input probes run under the live install hold before rehearsal",
            },
        ),
    ] = ()
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
