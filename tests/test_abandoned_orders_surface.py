"""A stopped name does not hold an order, and the board may not say otherwise."""

from __future__ import annotations

from agent_comms.ui_model.hud_groups import ParkedGroup
from agent_comms.ui_model.teammate_state import (
    Detached,
    NeedsTeamAttention,
    TeammateState,
)


def test_a_stopped_holder_asks_for_a_hand() -> None:
    """observations() keeps a unit that is not receiving exactly when it holds orders,
    so this card is always a holder. It used to sit in ParkedGroup below the working
    groups and raise no line, while readers took "a name is beside it" for "taken"."""
    assert issubclass(Detached, NeedsTeamAttention)
    assert Detached.attention() is True
    assert Detached.hud_group.needs_hand() is True


def test_nothing_that_holds_work_sits_below_the_board() -> None:
    """ParkedGroup raises no line. Only a state that cannot hold orders may live there."""
    parked = [
        member for member in TeammateState.worst_first()
        if member.hud_group is ParkedGroup
    ]
    assert parked == [], (
        "a card below the board must not be reachable while holding orders; found "
        + repr(parked)
    )


def test_no_urgent_state_offers_leaving_work_alone() -> None:
    """The action answers the card's question. This one used to read 'leave it parked,
    or reassign an order that cannot wait': the weakest reading, written down as the
    recommendation."""
    for member in TeammateState.worst_first():
        if not member.attention():
            continue
        assert "leave it" not in member.action.lower(), (
            f"{member.__name__} asks for a hand and then offers to do nothing: {member.action}"
        )
