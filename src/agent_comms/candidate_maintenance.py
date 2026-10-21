"""Index successful appends after outer locks release, within the owning root's lifetime."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from .bus import Bus
from .coordination_errors import CoordinationError
from .wake_candidate_index import ProjectionUnavailableError, WakeCandidateIndex

_LOG = logging.getLogger(__name__)
_MAX_ACTIVE_ROOTS = 32
_guard = threading.Lock()
_pending: dict[Path, CandidateMaintenance] = {}


class CandidateMaintenanceFailed(RuntimeError):  # noqa: N818 - named worker failure
    """One optional index drain failed; a later append may start a fresh drain."""

    def __init__(self, root: Path) -> None:
        self.root = root
        super().__init__(f"Candidate maintenance failed for {root}")


class CandidateMaintenance(threading.Thread):
    """One root's coalesced cursor and actual worker; retirement joins this owner."""

    def __init__(self, bus: Bus, seq: int) -> None:
        super().__init__(name="agent-comms-candidate-maintenance", daemon=True)
        self.bus = bus
        self.seq = seq

    def run(self) -> None:
        root = self.bus.path.parent
        try:
            index = WakeCandidateIndex(self.bus)
            while True:
                through = index.maintain()
                with _guard:
                    if self.seq <= through:
                        _pending.pop(root)
                        return
        except (
            OSError,
            ValueError,
            sqlite3.Error,
            CoordinationError,
            ProjectionUnavailableError,
        ) as error:
            failure = CandidateMaintenanceFailed(root)
            _LOG.exception("%s", failure)
            raise failure from error
        finally:
            with _guard:
                if root in _pending and _pending[root] is self:
                    _pending.pop(root)


def schedule_candidate_catchup(bus: Bus, seq: int) -> None:
    """Coalesce committed sequences into the root's one bounded scan worker."""
    if seq <= 0:
        raise ValueError("candidate notification needs an actual committed sequence")
    root = bus.path.parent
    with _guard:
        if root in _pending:
            worker = _pending[root]
            worker.seq = max(worker.seq, seq)
            return
        if len(_pending) >= _MAX_ACTIVE_ROOTS:
            raise CandidateMaintenanceFailed(root)
        worker = CandidateMaintenance(bus, seq)
        _pending[root] = worker
        try:
            worker.start()
        except RuntimeError as error:
            _pending.pop(root)
            raise CandidateMaintenanceFailed(root) from error


def drain_candidate_catchup(root: Path) -> None:
    """A stopped directory cannot be removed while workers on its roots still write."""
    while True:
        with _guard:
            workers = tuple(
                worker for path, worker in _pending.items() if path.is_relative_to(root)
            )
        if not workers:
            return
        for worker in workers:
            worker.join()
