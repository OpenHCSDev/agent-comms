"""Registration shared by service-owned message senders."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from .bus_publication import ServiceOrigin
from .repo import Checkout
from .threads import ServiceThread
from .thread_identity import ServiceThreadRole

if TYPE_CHECKING:
    from .registration import Registration


class ServiceSender:
    sender: ClassVar[str]
    origin: ClassVar[ServiceOrigin] = ServiceOrigin()

    @classmethod
    def register(cls, registry: Registration) -> None:
        """Declare this publisher's actual lifetime before every publication.

        Operational declaration preserves the original identity/history. Presence alone
        does not prove a retained sender is still admitted or executable.
        """
        registry.declare(
            ServiceThread(
                cls.sender,
                frozenset(),
                Checkout(registry.store.path.parent),
                role=ServiceThreadRole)
        )
