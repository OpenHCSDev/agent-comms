"""Canonical owner attestation held through adaptive compaction mutations.

Registration rechecks process identity, generation, active turn and goal under
its lock. Native session fields are caller observations; the native writer CAS
validates them under the session fence. CompactionSource owns input/wire
correction evidence, independently of this registry receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["OwnerCompactionAttestation"]


@dataclass(frozen=True)
class OwnerCompactionAttestation:
    """Evidence snapshot proving canonical owner authority at one instant.

    Produced only by ``Registration.attest_owner_compaction`` while holding
    the registry store lock. ``session_*`` fields are echoed caller values,
    not registry observations.
    """

    thread: str
    owner_generation: int = field(metadata={"wire_name": "owner_epoch"})
    turn_id: str
    goal_id: str | None
    goal_revision: int | None
    session_file: str
    session_leaf: str
    session_revision: str
    registry_revision: tuple[int, int, int, int] | None
