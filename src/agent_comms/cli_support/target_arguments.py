from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.messaging.channel_targets import NoChannel, SurroundingChannel
from agent_comms.nominal.field_codec import Argument, Normalize
from agent_comms.projects.checkouts import Checkout


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
