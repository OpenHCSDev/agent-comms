"""Per-thread heartbeat publication, independent of membership and observation."""

from collections.abc import Callable

from .nominal.declared_family import DeclaredFamily


class HeartbeatDelivery(DeclaredFamily, affix="HeartbeatDelivery"):
    """An observed board needs no bus publication by default."""

    @classmethod
    def publish[Publication](cls, operation: Callable[[], Publication]) -> tuple[Publication, ...]:
        return ()


class OnHeartbeatDelivery(HeartbeatDelivery):
    """Execute the packet owner's publication, retaining its notice or wake behavior."""

    @classmethod
    def publish[Publication](cls, operation: Callable[[], Publication]) -> tuple[Publication, ...]:
        return (operation(),)


class OffHeartbeatDelivery(HeartbeatDelivery):
    """The observation and HUD remain current; no bus message is created."""
