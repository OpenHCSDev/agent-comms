"""Private candidate maintenance is post-commit, optional, and never authority."""

from __future__ import annotations

from conftest import publish_initial
from agent_comms.read_basis import NamedViewer
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import agent_comms.candidate_maintenance as maintenance
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from agent_comms.wake_candidate_index import (
    WakeCandidateIndex, FirstBuild,
)
from agent_comms.wake_policy import FullWake

_SCHEDULE = maintenance.schedule_candidate_catchup

pytestmark = pytest.mark.skipif(os.name != "posix", reason="private bus requires POSIX")


def _wire(base: Path) -> tuple[Comms, str]:
    root = base / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    comms.registry.declare(
        comms.registry.new_thread('sender', frozenset(), str(base), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    comms.registry.declare(
        comms.registry.new_thread('beta', frozenset({'team'}), str(base), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    return comms, comms.messaging.initialize_private_initial_protocol()


def test_ordinary_private_send_deferred_index_catches_up_without_wake_authority(
    monkeypatch, tmp_path
) -> None:
    # Canonicalize symlinked temporary roots (for example macOS /var).
    with TemporaryDirectory(
        prefix="ac-candidate-scheduled-", dir=tmp_path.resolve(strict=True)
    ) as dirname:
        comms, root_id = _wire(Path(dirname))
        monkeypatch.setattr(
            "agent_comms.candidate_maintenance.schedule_candidate_catchup",
            _SCHEDULE,
        )
        indexed = threading.Event()
        original = maintenance._drain_candidate

        def observe(root):
            original(root)
            # Candidate catch-up alone is not completion: the same worker
            # still owns route preparation and its final pending-root check.
            indexed.set()

        monkeypatch.setattr(maintenance, "_drain_candidate", observe)
        message = comms.messaging.send_message("sender", "beta", "selected original")
        assert indexed.wait(timeout=5), "deferred checkpoint did not catch up"
        page = WakeCandidateIndex(comms.bus.log).page(
            root_id=root_id,
            recipient_lookup=RecordedThreadIncarnation("fixture", comms.registry.require("beta").created_at).lookup,
            after_seq=0,
            required_through_seq=message.seq,
        )
        assert page.through_seq >= message.seq and len(page.entries) == 1
        assert page.entries[0].source_seq == message.seq
        assert page.entries[0].wake_mode is FullWake


def test_notification_runs_after_wire_and_bus_locks_and_scheduler_defect_raises(
    monkeypatch, tmp_path
) -> None:
    with TemporaryDirectory(
        prefix="ac-candidate-postcommit-", dir=tmp_path.resolve(strict=True)
    ) as dirname:
        comms, _root_id = _wire(Path(dirname))
        witnessed: list[bool] = []

        def observe(log, through_bytes):
            def probe():
                with (
                    _store_lock(comms._wire_lock_path, blocking=False),
                    log.locked(blocking=False),
                ):
                    witnessed.append(True)

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=2)
            assert not thread.is_alive() and witnessed == [True]
            raise OSError("synthetic derived projection failure")

        monkeypatch.setattr(
            "agent_comms.candidate_maintenance.schedule_candidate_catchup", observe
        )
        with pytest.raises(OSError, match="synthetic derived projection failure"):
            comms.messaging.send_message("sender", "beta", "still committed")
        # The scheduler defect is visible only after physical lock release; the
        # durable append exists and its caller must treat the outcome as unknown.
        assert comms.bus.log.latest_sequence() == 1


def test_failed_drain_is_named_and_retires_pending_state(monkeypatch, tmp_path, caplog) -> None:
    comms, _root_id = _wire(tmp_path)
    root = comms.bus.log.path.parent
    maintenance._pending[root] = (comms.bus.log, 1)

    def fail(*args, **kwargs):
        raise ValueError("synthetic index failure")

    monkeypatch.setattr(WakeCandidateIndex, "catch_up_committed_append", fail)
    with pytest.raises(maintenance.CandidateMaintenanceFailed) as failed:
        maintenance._drain_candidate(root)
    assert failed.value.root == root
    assert root not in maintenance._pending
    assert "Candidate maintenance failed for" in caplog.text


def test_one_background_drain_catches_up_a_backlog_of_oversized_records(
    monkeypatch, tmp_path
) -> None:
    with TemporaryDirectory(
        prefix="ac-candidate-drain-", dir=tmp_path.resolve(strict=True)
    ) as dirname:
        comms, root_id = _wire(Path(dirname))
        monkeypatch.setattr(
            "agent_comms.candidate_maintenance.schedule_candidate_catchup", lambda *_: None
        )
        # A stalled index behind more oversized records than one bounded
        # 32-batch drain could previously cover: the background drain must
        # keep going until the index is caught up, not stop at its old
        # per-delivery batch budget.
        for number in range(40):
            publish_initial(comms, 
                "sender", "beta", f"{number:04d} " + "x" * (300 * 1024)
            )
        assert comms.bus.log.path.stat().st_size > 40 * 256 * 1024
        index = WakeCandidateIndex(comms.bus.log)
        with index.log.path.open("rb") as stream:
            first_bytes = len(stream.readline())
        assert index.catch_up_committed_append(
            index.notify_committed_append(root_id=root_id, through_bytes=first_bytes),
            start=FirstBuild(),
        ).checkpoint_bytes == first_bytes
        through_bytes = index.log.path.stat().st_size
        hint = index.notify_committed_append(root_id=root_id, through_bytes=through_bytes)
        maintenance._pending[index.log.path.parent] = (index.log, through_bytes)
        maintenance._drain_candidate(comms.bus.log.path.parent)
        assert not maintenance._pending
        assert index.catch_up_committed_append(hint).caught_up
        page = index.page(
            root_id=root_id,
            recipient_lookup=RecordedThreadIncarnation("fixture", comms.registry.require("beta").created_at).lookup,
            after_seq=0,
            required_through_seq=list(comms.bus.full_history_page().messages)[-1].seq,
            limit=100,
        )
        assert len(page.entries) >= 1 and not page.has_more


def test_concurrent_ordinary_publication_keeps_maintenance_alive(monkeypatch, tmp_path):
    comms, root_id = _wire(tmp_path)
    root = comms.bus.log.path.parent
    finished = threading.Condition()
    failures = []
    drain = maintenance._drain_candidate

    def observed_drain(path):
        try:
            drain(path)
        except Exception as error:
            failures.append(error)
            raise
        finally:
            with finished:
                finished.notify_all()

    def retired():
        with maintenance._guard:
            return root not in maintenance._pending

    monkeypatch.setattr(maintenance, "_drain_candidate", observed_drain)
    monkeypatch.setattr(maintenance, "schedule_candidate_catchup", _SCHEDULE)
    messages = [comms.messaging.send_message("sender", "beta", str(n) + "x" * (128 * 1024))
                for n in range(128)]
    with finished:
        assert finished.wait_for(retired, timeout=15), "maintenance did not finish"
    assert failures == []
    index = WakeCandidateIndex(comms.bus.log)
    assert index._verified_checkpoint(root_id).last_seq == messages[-1].seq
    page = index.page(root_id=root_id,
                      recipient_lookup=RecordedThreadIncarnation("fixture", comms.registry.require("beta").created_at).lookup,
                      after_seq=0, required_through_seq=messages[-1].seq, limit=32)
    assert len(page.entries) == 32 and page.has_more
    with comms.bus.snapshot(viewer=NamedViewer("beta")) as (basis, source):
        assert comms.bus.pending_counts_opened(source, basis) == {"sender": len(messages)}
