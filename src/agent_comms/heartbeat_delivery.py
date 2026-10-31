"""Per-thread heartbeat publication, independent of membership and observation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .channel_targets import ThreadTarget
from .nominal.declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .comms import Comms
    from .messages import Message
    from .task_attachments.commander_pings import CommanderUpdate


class HeartbeatDelivery(DeclaredFamily, affix="HeartbeatDelivery"):
    """An observed board needs no bus publication by default."""

    @classmethod
    def publish(cls, update: CommanderUpdate, comms: Comms,
                hud: tuple[str, ...]) -> tuple[Message, ...]:
        return ()


class OnHeartbeatDelivery(HeartbeatDelivery):
    """Publish the board with its own normal notice or wake behavior."""

    @classmethod
    def publish(cls, update: CommanderUpdate, comms: Comms,
                hud: tuple[str, ...]) -> tuple[Message, ...]:
        return (update.publish(
            comms.messaging, ThreadTarget(update.commander.name), update.body(hud)),)


class OffHeartbeatDelivery(HeartbeatDelivery):
    """The observation and HUD remain current; no bus message is created."""
