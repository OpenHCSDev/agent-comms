"""Registration shared by service-owned message senders."""

from __future__ import annotations

from typing import ClassVar

from .registration import Registration
from .thread_execution import ExternalThreadExecution


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
