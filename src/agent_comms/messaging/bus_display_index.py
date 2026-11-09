from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)
from typing import Annotated

from agent_comms.activity_clocks import ChannelActivity
from agent_comms.bus_activity_index import ActivityMaximum
from agent_comms.storage.typed_table import Column


@dataclass(frozen=True)
class BusDisplayIndex(ChannelActivity, ActivityMaximum):
    participant: Annotated[str, Column(primary_key=True)] = field(kw_only=True)
