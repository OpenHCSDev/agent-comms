"""What an order serves, independent of concrete plan evidence and events."""

from __future__ import annotations

from collections.abc import Callable

from agent_comms.nominal.declared_family import DeclaredFamily


class OrderPhase(DeclaredFamily, affix="Phase", discovery_package='agent_comms.planning.plan_phases'):
    """What an order serves: one plan phase, or nothing in any plan."""

    @classmethod
    def order_refusals(cls, read_ready: Callable[[], tuple[type[OrderPhase], ...]],
                       reason: str) -> str | None:
        return None


class UnplannedPhase(OrderPhase):
    @classmethod
    def order_refusals(cls, read_ready: Callable[[], tuple[type[OrderPhase], ...]],
                       reason: str) -> str | None:
        """Read the heartbeat's ready phases only when the stated reason does not answer the
        refusal."""
        if len(reason.split()) >= 8:
            return None
        ready = read_ready()
        if ready:
            return (
                "Plan phases are ready: "
                + ", ".join(phase.display_name() for phase in ready)
                + ". Name a phase, or provide an explicit unplanned_reason of 8+ words."
            )
        return None
