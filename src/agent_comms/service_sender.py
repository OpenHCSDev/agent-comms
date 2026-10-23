"""Registration shared by service-owned message senders."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from .thread_execution import ExternalThreadExecution

if TYPE_CHECKING:
    from .registration import Registration


class ServiceSender:
    sender: ClassVar[str]

    @classmethod
    def register(cls, registry: Registration) -> None:
        """Declare the service sender before its first publication."""
        if cls.sender not in registry:
            registry.declare(
                registry.new_thread(
                    cls.sender,
                    frozenset(),
                    str(registry.store.path.parent),
                    execution=ExternalThreadExecution,
                )
            )
