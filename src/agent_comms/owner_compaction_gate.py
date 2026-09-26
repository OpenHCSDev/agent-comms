"""Canonical owner/correction attestation gate for adaptive compaction commits.

Dormant by design: nothing in the runtime calls this yet. The gate exists so a
future Python↔Pi bridge can obtain owner evidence from the canonical
``ThreadRegistry`` instead of trusting caller-stamped labels.

Design invariants (blocker 1 of the PR48 merge plan):

- An attestation is a *recheck under the registry lock*, never a bearer
  token. The identical recheck must run again at commit time; any owner-epoch,
  active-turn, goal id/revision/status, or liveness change in between fails
  closed with :class:`RelationViolationError`.
- ``correction_revision`` is bound as an opaque owner-supplied counter. The
  registry does not own a global correction ledger; staleness detection comes
  from the live epoch/turn recheck, and the value is echoed verbatim so the
  consuming bridge can compare it against its own correction evidence.
- The native session fence (session file, leaf entry ID, disk revision) is
  echoed **unverified**. Only the native writer CAS under the per-session
  lock is authority for those values; the registry must never assert them.
- The returned ``registry_revision`` identifies the exact registry store
  revision that produced the attestation, so reviewers and bridges can tell
  whether two attestations came from the same store state.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["OwnerCompactionAttestation"]


@dataclass(frozen=True)
class OwnerCompactionAttestation:
    """Evidence snapshot proving canonical owner authority at one instant.

    Produced only by ``ThreadRegistry.attest_owner_compaction`` while holding
    the registry store lock. ``session_*`` fields are echoed caller values,
    not registry observations.
    """

    thread: str
    owner_epoch: int
    turn_id: str
    goal_id: str
    goal_revision: int
    correction_revision: int
    session_file: str
    session_leaf: str
    session_revision: str
    registry_revision: tuple[int, int, int, int] | None
