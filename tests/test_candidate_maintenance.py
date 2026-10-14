"""Private candidate maintenance is post-commit, optional, and never authority."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms import candidate_maintenance as maintenance
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from agent_comms.wake_candidate_index import WakeCandidateIndex

pytestmark = pytest.mark.skipif(os.name != "posix", reason="private bus requires POSIX")


def _wire(base: Path) -> tuple[Comms, str]:
    root = base / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    comms.registry.declare(
        Thread(
            "sender", frozenset(), str(base), process_identity=ProcessIdentity.capture(os.getpid())
        )
    )
    comms.registry.declare(
        Thread(
            "beta",
            frozenset({"team"}),
            str(base),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
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
            "agent_comms.messaging.schedule_candidate_catchup",
            maintenance.schedule_candidate_catchup,
        )
        indexed = threading.Event()
        original = WakeCandidateIndex.catch_up_committed_append

        def observe(self, hint, **kwargs):
            result = original(self, hint, **kwargs)
            if result.caught_up:
                indexed.set()
            return result

        monkeypatch.setattr(WakeCandidateIndex, "catch_up_committed_append", observe)
        message = comms.messaging.send_message("sender", "beta", "selected original")
        assert indexed.wait(timeout=5), "deferred checkpoint did not catch up"
        page = WakeCandidateIndex(comms.bus).page(
            root_id=root_id,
            recipient_lookup=stable_thread_lookup(comms.registry.require("beta").created_at),
            after_seq=0,
            required_through_seq=message.seq,
        )
        assert page.through_seq >= message.seq and len(page.entries) == 1
        assert page.entries[0].source_seq == message.seq
        assert page.entries[0].wake_mode == "full"


def test_failed_drain_is_named_and_retires_pending_state(monkeypatch, tmp_path, caplog) -> None:
    comms, _root_id = _wire(tmp_path)
    root = comms.bus.log.path.parent
    maintenance._pending[root] = (comms.bus, 1)

    def fail(*args, **kwargs):
        raise ValueError("synthetic index failure")

    monkeypatch.setattr(WakeCandidateIndex, "catch_up_committed_append", fail)
    with pytest.raises(maintenance.CandidateMaintenanceFailed) as failed:
        maintenance._drain_candidate(root)
    assert failed.value.root == root
    assert root not in maintenance._pending
    assert "Candidate maintenance failed for" in caplog.text


def test_notification_runs_after_wire_and_bus_locks_and_failure_cannot_fail_send(
    monkeypatch, tmp_path
) -> None:
    with TemporaryDirectory(
        prefix="ac-candidate-postcommit-", dir=tmp_path.resolve(strict=True)
    ) as dirname:
        comms, _root_id = _wire(Path(dirname))
        witnessed: list[bool] = []

        def observe(bus, seq):
            def probe():
                with (
                    _store_lock(comms._wire_lock_path, blocking=False),
                    _store_lock(bus.log.path, blocking=False),
                ):
                    witnessed.append(True)

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=2)
            assert not thread.is_alive() and witnessed == [True]
            raise OSError("synthetic derived projection failure")

        monkeypatch.setattr(
            "agent_comms.messaging.schedule_candidate_catchup", observe
        )
        # Even an unexpected scheduler error after the durable bus commit
        # cannot report the original send as failed or invite a retry.
        committed = comms.messaging.send_message("sender", "beta", "still committed")
        assert committed.seq == 1 and comms.bus.log.latest_sequence() == 1


def test_one_background_drain_catches_up_a_backlog_of_oversized_records(
    monkeypatch, tmp_path
) -> None:
    with TemporaryDirectory(
        prefix="ac-candidate-drain-", dir=tmp_path.resolve(strict=True)
    ) as dirname:
        comms, root_id = _wire(Path(dirname))
        monkeypatch.setattr(
            "agent_comms.messaging.schedule_candidate_catchup", lambda *_: None
        )
        # A stalled index behind more oversized records than one bounded
        # 32-batch drain could previously cover: the background drain must
        # keep going until the index is caught up, not stop at its old
        # per-delivery batch budget.
        for number in range(40):
            comms.messaging.send_initial_cohort(
                "sender", "beta", f"{number:04d} " + "x" * (300 * 1024)
            )
        assert comms.bus.log.path.stat().st_size > 40 * 256 * 1024
        index = WakeCandidateIndex(comms.bus)
        assert index.catch_up_committed_append(
            index.notify_committed_append(root_id=root_id, through_seq=1),
            bootstrap_new=True,
        ).checkpoint_seq == 1
        hint = index.notify_committed_append(
            root_id=root_id, through_seq=comms.bus.log.full_history()[-1].seq
        )
        maintenance._pending[comms.bus.log.path.parent] = (
            comms.bus,
            comms.bus.log.full_history()[-1].seq,
        )
        maintenance._drain_candidate(comms.bus.log.path.parent)
        assert not maintenance._pending
        assert index.catch_up_committed_append(hint).caught_up
        page = index.page(
            root_id=root_id,
            recipient_lookup=stable_thread_lookup(
                comms.registry.require("beta").created_at
            ),
            after_seq=0,
            required_through_seq=comms.bus.log.full_history()[-1].seq,
            limit=100,
        )
        assert len(page.entries) >= 1 and not page.has_more
