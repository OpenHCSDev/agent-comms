from __future__ import annotations

from agent_comms.channel_targets import NoChannel
from agent_comms.channel_targets import SurroundingChannel
from agent_comms.cli_command import CliCommand
from agent_comms.repo import Checkout
from agent_comms.field_codec import Argument
from agent_comms.field_codec import Normalize
from dataclasses import dataclass
from typing import Annotated
import dataclasses


@dataclass(frozen=True, kw_only=True)
class TargetArguments(CliCommand):
    project: Annotated[Checkout, Argument(flags=("--project",))]
    channel: Annotated[SurroundingChannel, Argument(flags=("--channel",))] = NoChannel()
    target: Annotated[
        tuple[str, ...],
        Normalize(list),
        Argument(
            flags=(
                "--target",
                "--targets",
            ),
            options={"nargs": "+"},
        ),
    ] = dataclasses.field(default_factory=tuple)
