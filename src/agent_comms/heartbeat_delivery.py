"""Per-thread heartbeat delivery, independent of commander membership and observation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .nominal.declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .task_attachments.commander_pings import CommanderUpdate


class HeartbeatDelivery(DeclaredFamily, affix="HeartbeatDelivery"):
    """The selected policy turns an observed board into its deliverable packet."""

    @classmethod
    def packet(cls, update: CommanderUpdate) -> CommanderUpdate:
        return update


class OnHeartbeatDelivery(HeartbeatDelivery):
    """Keep the board's normal wake decision."""


class OffHeartbeatDelivery(HeartbeatDelivery):
    """Retain the board without sending it as model input."""

    @classmethod
    def packet(cls, update: CommanderUpdate) -> CommanderUpdate:
        return update.passive()
