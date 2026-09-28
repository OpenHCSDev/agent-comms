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
    comms.threads.register(
        Thread(
            "sender", frozenset(), str(base), process_identity=ProcessIdentity.capture(os.getpid())
        )
    )
    comms.threads.register(
        Thread(
            "beta",
            frozenset({"team"}),
            str(base),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    return comms, comms.messaging.initialize_private_initial_protocol()


def test_ordinary_private_send_deferred_index_catches_up_without_wake_authority(
    monkeypatch,
) -> None:
    with TemporaryDirectory(prefix="ac-candidate-scheduled-", dir="/var/tmp") as dirname:
        comms, root_id = _wire(Path(dirname))
        monkeypatch.setattr(
            "agent_comms.messaging.schedule_private_candidate_after_commit",
            maintenance.schedule_private_candidate_after_commit,
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


def test_notification_runs_after_wire_and_bus_locks_and_failure_cannot_fail_send(
    monkeypatch,
) -> None:
    with TemporaryDirectory(prefix="ac-candidate-postcommit-", dir="/var/tmp") as dirname:
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
            "agent_comms.messaging.schedule_private_candidate_after_commit", observe
        )
        # Even an unexpected scheduler error after the durable bus commit
        # cannot report the original send as failed or invite a retry.
        committed = comms.messaging.send_message("sender", "beta", "still committed")
        assert committed.seq == 1 and comms.bus.log.latest_sequence() == 1
