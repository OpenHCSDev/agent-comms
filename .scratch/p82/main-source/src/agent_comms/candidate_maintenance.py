"""Best-effort indexing of successful appends after the outer locks release.

Losing a hint or crashing the daemon leaves the optional index unavailable;
canonical bus and sealed SQL still drive original delivery. Byte positions
include observations as well as messages, without inventing message sequences.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .bus_route_counts import BusRouteCounts
from .routing import DeliveryMessage
from .wire_log import WireLog
from .wake_candidate_index import WakeCandidateIndex

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
    if type(log) is not WireLog or type(through_bytes) is not int or through_bytes <= 0:
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
        except RuntimeError:
            _pending.pop(root, None)
            _LOG.warning("Candidate notification omitted: maintenance worker unavailable")


def _drain_candidate(root: Path) -> None:
    try:
        with _guard:
            log, _bytes = _pending[root]
        # Source ownership is checked off the producer path. Maintenance cannot
        # create the protocol marker or authorize delivery.
        with log.certified_read() as source:
            metadata = source.marker
        root_id = metadata.root_id
        index = WakeCandidateIndex(log)
        routes = BusRouteCounts(log.path)
        while True:
            with _guard:
                target = _pending[root][1]
            hint = index.notify_committed_append(root_id=root_id, through_bytes=target)
            result = index.catch_up_committed_append(
                hint, bootstrap_new=True
            )
            with log._opened_wire_snapshot(need_sequence=False) as source:
                route_result = routes.sync(
                    source.stream,
                    source.revision,
                    lambda record: DeliveryMessage.from_wire(record, root_id=source.metadata.root_id),
                )
            if result.caught_up and route_result.complete:
                with _guard:
                    if _pending[root][1] <= result.checkpoint_bytes:
                        # Retire this worker once, atomically with its final
                        # target check. A later append can start a new worker;
                        # this worker must not remove that successor's signal.
                        _pending.pop(root)
                        return
    except Exception as error:
        with _guard:
            _pending.pop(root, None)
        # The worker thread has no caller. Publish one named failure through the
        # log and threading.excepthook after retiring this process-local drain.
        failure = CandidateMaintenanceFailed(root)
        _LOG.exception("%s", failure)
        raise failure from error
