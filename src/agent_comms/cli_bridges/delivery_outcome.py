"""What one harness delivery attempt established: its outcome family.

Kept below ``harness_delivery_states`` and ``messaging.task_attachment`` so
lightweight message and send paths do not pull in notification progress,
assignment states, or coordinator tables.
"""

from __future__ import annotations

import sqlite3

from agent_comms.nominal.declared_family import DeclaredFamily


class Outcome(DeclaredFamily, affix="Outcome"):
    """Stored spellings stay the same; the stored outcome decides what may follow it."""

    @classmethod
    def settle[Delivery](
        cls, successor: Delivery, assignments: object, db: sqlite3.Connection,
    ) -> Delivery:
        """An open attempt takes the successor and the claim steps its outcome proves."""
        successor.record(assignments, db)
        return successor


class FilteredOutcome(Outcome):
    @classmethod
    def settle[Delivery](
        cls, successor: Delivery, assignments: object, db: sqlite3.Connection,
    ) -> Delivery:
        """A settled attempt stays settled; a later emission or observation cannot change it."""
        del assignments
        stored, = successor.stored(db)
        return stored


class UnconfirmedOutcome(Outcome):
    pass


class DeliveredOutcome(FilteredOutcome):
    """An original harness response, not evidence that a model took the body.

    Older generic responses retain this spelling and their actual monitor proof.
    A producer name alone cannot upgrade their evidence to model context.
    """


class AcceptedOutcome(Outcome):
    pass


class PersistedOutcome(AcceptedOutcome):
    pass


class EmittedOutcome(UnconfirmedOutcome):
    pass
