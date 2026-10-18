"""Fresh isolated application append -> complete read -> candidate maintenance.

No provider, saved session or retained input is involved. Refuse an existing
root so a failed run is evidence, never an operation to replay for clean output.
Run with the intended package/interpreter; the receipt identifies the import.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import agent_comms
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.threads import Thread
from agent_comms.wake_candidate_index import WakeCandidateIndex
from agent_comms.wire_record import WireRecord


def main(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True)
    comms = Comms(root / "wire")
    births = {"sender": 17001.0, "Alice": 17002.0}
    for name, birth in births.items():
        comms.registry.declare(Thread(
            name, frozenset({"team"}), str(root), created_at=birth,
            process_identity=ProcessIdentity.capture(os.getpid()),
        ))
    root_id = comms.messaging.initialize_private_initial_protocol()
    message = comms.messaging.send_initial_cohort("sender", "Alice", "physical row " * 1024)
    # Record the completed effect before any subsequent check can fail.
    (root / "effect.json").write_text(json.dumps({
        "root_id": root_id, "message_id": message.message_id, "seq": message.seq,
    }) + "\n")
    # Retire this run's real post-commit worker, not a replacement scheduler.
    for worker in threading.enumerate():
        if worker.name == "agent-comms-candidate-maintenance":
            worker.join(timeout=10)
            assert not worker.is_alive(), "candidate worker did not retire"
    log = comms.bus.log
    with log.verified_snapshot() as records:
        retained = tuple(records)
    assert len(retained) == 1
    assert retained[0].messages() == (message,)
    before = log.path.read_bytes()
    assert 1 < len(before) <= WireRecord.max_row_bytes
    index = WakeCandidateIndex(log)
    # The actual row exceeds this work quantum but is a legal physical row.
    # Explicit rebuild exercises the first-row exception without racing the
    # already-retired production maintenance worker.
    assert index.maintain(rebuild=True, max_bytes=1)
    page = index.page(
        root_id=root_id, recipient_lookup=RecordedThreadIncarnation("fixture", births["Alice"]).lookup,
        after_seq=0, required_through_seq=message.seq,
    )
    assert len(page.entries) == 1
    assert page.entries[0].message_id == message.message_id
    assert log.path.read_bytes() == before
    receipt = {
        "accepted": True, "package": agent_comms.__file__, "python": sys.executable,
        "physical_limit": WireRecord.max_row_bytes, "row_bytes": len(before),
        "batch_bytes": 1, "complete_read": True, "candidate_page": True,
        "original_written_bytes_unchanged": True, "provider_called": False,
    }
    (root / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
