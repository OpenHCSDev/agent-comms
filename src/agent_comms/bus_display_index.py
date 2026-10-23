"""Participant activity maxima share the bus append transaction."""
from __future__ import annotations

from .activity_clocks import ChannelActivity
from dataclasses import dataclass, field
from typing import Annotated

from .bus_activity_index import ActivityMaximum
from .typed_table import Column


@dataclass(frozen=True)
class BusDisplayIndex(ChannelActivity, ActivityMaximum):
    participant: Annotated[str, Column(primary_key=True)] = field(kw_only=True)
