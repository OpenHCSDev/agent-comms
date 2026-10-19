"""Best-effort indexing of successful appends after the outer locks release.

Losing a hint or crashing the daemon leaves the optional index unavailable;
canonical bus and sealed SQL still drive original delivery. Byte positions
include observations as well as messages, without inventing message sequences.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import ExitStack
from pathlib import Path

from .bus_route_counts import BusRouteCounts
from .coordination_errors import CoordinationError
from .routing import DeliveryMessage
from .wire_log import CurrentSnapshotOpening, WireLog
from .wake_candidate_index import FirstBuild, ProjectionUnavailableError, WakeCandidateIndex

_LOG = logging.getLogger(__name__)
_MAX_ACTIVE_ROOTS = 32
_guard = threading.Lock()
_pending: dict[Path, tuple[WireLog, int]] = {}


class CandidateMaintenanceFailed(RuntimeError):  # noqa: N818 - named worker failure
    """One optional index drain failed; a later append may start a fresh drain."""

    def __init__(self, root: Path) -> None:
        self.root = root
        super().__init__(f"Candidate maintenance failed for {root}")


def schedule_candidate_catchup(log: WireLog, through_bytes: int) -> None:
    """Prepare writer indexes, then signal their existing maintenance worker."""
    if through_bytes <= 0:
        raise ValueError("candidate notification needs an actual committed byte position")
    BusRouteCounts(log.path).prepare()
    root = log.path.parent
    with _guard:
        current = _pending.get(root)
        if current is not None:
            _pending[root] = (current[0], max(current[1], through_bytes))
            return
        if len(_pending) >= _MAX_ACTIVE_ROOTS:
            raise CandidateMaintenanceFailed(root)
        _pending[root] = (log, through_bytes)
        try:
            threading.Thread(
                target=_drain_candidate,
                args=(root,),
                name="agent-comms-candidate-maintenance",
                daemon=True,
            ).start()
        except RuntimeError as error:
            _pending.pop(root, None)
            raise CandidateMaintenanceFailed(root) from error


def _retire_candidate(root: Path) -> None:
    with _guard:
        _pending.pop(root, None)


def _drain_candidate(root: Path) -> None:
    with ExitStack() as cleanup:
        cleanup.callback(_retire_candidate, root)
        try:
            with _guard:
                log, _bytes = _pending[root]
            # Source ownership is checked off the producer path. Maintenance cannot
            # create the protocol marker or authorize delivery.
            with log.checked_read() as source:
                metadata = source.marker
            root_id = metadata.root_id
            index = WakeCandidateIndex(log)
            routes = BusRouteCounts(log.path)
            while True:
                with _guard:
                    target = _pending[root][1]
                hint = index.notify_committed_append(root_id=root_id, through_bytes=target)
                result = index.catch_up_committed_append(
                    hint, start=FirstBuild()
                )
                with log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as snapshot:
                    route_result = routes.sync(
                        snapshot,
                        lambda record: DeliveryMessage.from_wire(record, root_id=snapshot.metadata.root_id),
                    )
                if result.caught_up and route_result.complete:
                    with _guard:
                        if _pending[root][1] <= result.checkpoint_bytes:
                            # Disarm cleanup before a successor can own this root.
                            cleanup.pop_all()
                            _pending.pop(root)
                            return
        except (OSError, ValueError, sqlite3.Error, CoordinationError, ProjectionUnavailableError) as error:
            failure = CandidateMaintenanceFailed(root)
            _LOG.exception("%s", failure)
            raise failure from error
