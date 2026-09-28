"""Best-effort post-publication candidate indexing; never delivery authority.

The publisher gives this scheduler only a durable sequence after releasing the
wire and bus locks. Losing a hint or crashing this daemon leaves the WAL index
stale/unavailable; canonical bus and sealed SQL still drive original delivery.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .message_bus import MessageBus
from .wake_candidate_index import WakeCandidateIndex

_LOG = logging.getLogger(__name__)
_MAX_ACTIVE_ROOTS = 32
_MAX_BATCHES = 32
_guard = threading.Lock()
_pending: dict[Path, tuple[MessageBus, int]] = {}


def schedule_private_candidate_after_commit(bus: MessageBus, committed_seq: int) -> None:
    """Ignore legacy roots and never let optional maintenance fail a committed send."""
    try:
        marker = bus.log.path.parent / "bus_meta.json"
        info = marker.stat()
        if info.st_size > 4096 or b'"writer_protocol_version"' not in marker.read_bytes():
            return
        schedule_candidate_catchup(bus, committed_seq)
    except Exception as error:
        _LOG.warning(
            "Candidate notification omitted after committed send (%s)", type(error).__name__
        )


def schedule_candidate_catchup(bus: MessageBus, committed_seq: int) -> None:
    """A bounded memory-only signal; never wait for WAL or a wire lock here."""
    if type(bus) is not MessageBus or type(committed_seq) is not int or committed_seq <= 0:
        raise ValueError("candidate notification needs an actual committed bus sequence")
    root = bus.log.path.parent
    with _guard:
        current = _pending.get(root)
        if current is not None:
            _pending[root] = (current[0], max(current[1], committed_seq))
            return
        if len(_pending) >= _MAX_ACTIVE_ROOTS:
            _LOG.warning("Candidate notification omitted: maintenance root budget exceeded")
            return
        _pending[root] = (bus, committed_seq)
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
            bus, _seq = _pending[root]
        # This check is deliberately off the producer path. A private marker
        # may not exist on a legacy send; never create one or activate private
        # processing by merely scheduling derived maintenance.
        with bus.log.locked():
            metadata = bus.log._private_marker_unlocked()
        root_id = str(metadata["wire_root_id"])
        index = WakeCandidateIndex(bus)
        for _ in range(_MAX_BATCHES):
            with _guard:
                target = _pending[root][1]
            hint = index.notify_committed_append(root_id=root_id, through_seq=target)
            result = index.catch_up_committed_append(
                hint, max_rows=64, max_bytes=256 * 1024, bootstrap_new=True
            )
            if result.caught_up:
                with _guard:
                    if _pending[root][1] <= result.checkpoint_seq:
                        _pending.pop(root, None)
                        return
            elif not result.more_source_bytes:
                return
        _LOG.warning("Candidate maintenance stopped at its bounded batch budget")
    except Exception as error:
        _LOG.warning("Candidate maintenance unavailable (%s)", type(error).__name__)
    with _guard:
        _pending.pop(root, None)
