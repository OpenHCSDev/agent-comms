"""Index successful appends after outer locks release, within the owning root's lifetime."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from .bus_route_counts import BusRouteCounts
from .coordination_errors import CoordinationError
from .routing import DeliveryMessage
from .wire_log import CurrentSnapshotOpening, WireLog
from .wake_candidate_index import FirstBuild, ProjectionUnavailableError, WakeCandidateIndex

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

    def __init__(self, log: WireLog, through_bytes: int) -> None:
        super().__init__(name="agent-comms-candidate-maintenance", daemon=True)
        self.log = log
        self.through_bytes = through_bytes

    def run(self) -> None:
        root = self.log.path.parent
        try:
            with self.log.checked_read() as source:
                metadata = source.marker
            root_id = metadata.root_id
            index = WakeCandidateIndex(self.log)
            routes = BusRouteCounts(self.log.path)
            while True:
                with _guard:
                    target = self.through_bytes
                hint = index.notify_committed_append(root_id=root_id, through_bytes=target)
                result = index.catch_up_committed_append(hint, start=FirstBuild())
                with self.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as snapshot:
                    route_result = routes.sync(
                        snapshot,
                        lambda record: DeliveryMessage.from_wire(record, root_id=snapshot.metadata.root_id),
                    )
                if result.caught_up and route_result.complete:
                    with _guard:
                        if self.through_bytes <= result.checkpoint_bytes:
                            _pending.pop(root)
                            return
        except (OSError, ValueError, sqlite3.Error, CoordinationError, ProjectionUnavailableError) as error:
            failure = CandidateMaintenanceFailed(root)
            _LOG.exception("%s", failure)
            raise failure from error
        finally:
            with _guard:
                if root in _pending and _pending[root] is self:
                    _pending.pop(root)


def schedule_candidate_catchup(log: WireLog, through_bytes: int) -> None:
    """Prepare writer indexes, then coalesce into the root's actual worker."""
    if through_bytes <= 0:
        raise ValueError("candidate notification needs an actual committed byte position")
    BusRouteCounts(log.path).prepare()
    root = log.path.parent
    with _guard:
        if root in _pending:
            worker = _pending[root]
            worker.through_bytes = max(worker.through_bytes, through_bytes)
            return
        if len(_pending) >= _MAX_ACTIVE_ROOTS:
            raise CandidateMaintenanceFailed(root)
        worker = CandidateMaintenance(log, through_bytes)
        _pending[root] = worker
        try:
            worker.start()
        except RuntimeError as error:
            _pending.pop(root)
            raise CandidateMaintenanceFailed(root) from error


def drain_candidate_catchup(root: Path) -> None:
    """A stopped root cannot be renamed or removed while its worker still writes."""
    while True:
        with _guard:
            if root not in _pending:
                return
            worker = _pending[root]
        worker.join()
