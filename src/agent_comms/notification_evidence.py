"""Detached notification evidence and the bounded window family contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from itertools import batched
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from .audience_manifest import FrozenRecipient
    from .bus_publication import Delivered
    from .comms import Comms
    from .coordination_tables.assignments import WakeAssignment
    from .coordination_tables.harness_deliveries import HarnessDelivery
    from .field_codec import ThreadLookup
    from .message_reference import MessageReference
    from .notification_assignment import AssignmentActivityEvidence
    from .notification_progress import DeliveryProgress

    @property
    def sources(self) -> tuple[Delivered, ...]:
        return self.selection.sources
