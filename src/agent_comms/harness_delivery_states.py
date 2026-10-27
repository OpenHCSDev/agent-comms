"""What one harness delivery attempt established: its outcome family.

Kept below the coordinator tables in the import graph so the table that records
attempts can declare these families without importing the bridge transport.
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .cli_bridges.shared import CliBridge, DeliveryRow


class Outcome(DeclaredFamily, affix="Outcome"):
    """Stored spellings stay the same; their owners perform confirmation."""

    @classmethod
    async def record(cls, bridge: CliBridge, delivery: DeliveryRow) -> None:
        with bridge.db:
            replace(delivery, outcome=cls).upsert(bridge.db)

    @classmethod
    async def finish(
        cls, bridge: CliBridge, delivery: DeliveryRow, observation: type[Outcome]
    ) -> None:
        await observation.record(bridge, delivery)

    @classmethod
    def entered_context(cls) -> bool:
        """Whether this attempt proves the message entered the harness context; an emitted,
        unconfirmed or filtered attempt proves nothing."""
        return False


class FilteredOutcome(Outcome):
    @classmethod
    async def finish(
        cls, bridge: CliBridge, delivery: DeliveryRow, observation: type[Outcome]
    ) -> None:
        """The journal confirmed this row; a later emission cannot change that fact."""


class UnconfirmedOutcome(Outcome):
    pass


class DeliveredOutcome(FilteredOutcome):
    @classmethod
    async def record(cls, bridge: CliBridge, delivery: DeliveryRow) -> None:
        await bridge.comms.bus.when_open(partial(
            bridge.comms.bus.mark_delivered, bridge.binding.bus, (delivery.message.reference,)))
        await super().record(bridge, delivery)

    @classmethod
    def entered_context(cls) -> bool:
        return True


class EmittedOutcome(UnconfirmedOutcome):
    pass
