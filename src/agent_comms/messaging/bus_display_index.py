from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)
from typing import Annotated

from ..activity_clocks import ChannelActivity
from ..bus_activity_index import ActivityMaximum
from ..storage.typed_table import Column


@dataclass(frozen=True)
class BusDisplayIndex(ChannelActivity, ActivityMaximum):
    participant: Annotated[str, Column(primary_key=True)] = field(kw_only=True)
