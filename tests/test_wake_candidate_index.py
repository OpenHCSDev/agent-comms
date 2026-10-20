"""Bounded, disposable N/K candidate pages are never claim_scope evidences."""

from __future__ import annotations

from conftest import publish_initial
import json
import multiprocessing
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.bus_publication import (
    PRIVATE_WIRE_FIELD,
    public_envelope_digest,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import accept_delivery_cohort, sealed_cohort_assignments
from agent_comms.coordination_tables.publications import canonical_publication_key
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.private_path import FileRevision
from agent_comms.wire_log import SavedPrefixOpening
from agent_comms.wire_record import WireRecord
from agent_comms.threads import Thread
from agent_comms.typed_table import TypedTable
from agent_comms.wake_candidate_index import (
    CandidateCheckpoint, CandidateTable, ProjectionRebuildRequiredError,
    ProjectionUnavailableError, WakeCandidateIndex, FirstBuild, RebuildIndex, ResumeIndex,
)
from agent_comms.wake_policy import FullWake

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="private N/K bus requires POSIX")


def _maintain_in_process(root: str, root_id: str, through_bytes: int, start, results) -> None:
    """One independent runtime connection bootstrapping the shared WAL."""
    start.wait()
    try:
        index = WakeCandidateIndex(Comms(Path(root)).bus.log)
        hint = index.notify_committed_append(root_id=root_id, through_bytes=through_bytes)
        while True:
            outcome = index.catch_up_committed_append(
                hint, start=FirstBuild(), max_rows=256, max_bytes=8 * 1024 * 1024,
            )
            if outcome.caught_up:
                break
    except BaseException as error:
        results.put(("error", type(error).__name__, str(error)))
    else:
        results.put(("ok", outcome.caught_up))


@pytest.fixture
def participants(private_comms, tmp_path: Path) -> dict[str, str]:
    """This file's three team members on the shared private bus, by lookup."""
    created = {"sender": 17001.0, "Alice": 17002.0, "Bob": 17003.0}
    for name, identity in created.items():
        private_comms.registry.declare(
            private_comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), created_at=identity)
        )
    return {name: RecordedThreadIncarnation("fixture", identity).lookup for name, identity in created.items()}


def _replace_rows(comms: Comms, rows: list[dict]) -> None:
    path = comms.bus.log.path
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)


def _as_response(row, root_id, execution_id):
    """Build current declared response shape for raw-writer corruption tests."""
    from agent_comms.bus_publication import initial_sideband
    from agent_comms.delivery_policy import KeyedResponseAcknowledgement, ResponseDeliveryPolicy
    from agent_comms.field_codec import FieldCodec
    from agent_comms.wake import ControlClassification

    delivery = WireRecord.from_wire(row, root_id)
    public = delivery.message.to_wire()
    policy = ResponseDeliveryPolicy(
        version=1,
        initial=initial_sideband(
            root_id, delivery.message, delivery.audience,
            ResponseDeliveryPolicy.resolve(
                delivery.message, delivery.audience, ControlClassification.ORDINARY
            ),
            control=ControlClassification.ORDINARY.value,
        ),
        response=KeyedResponseAcknowledgement(
            root_id, execution_id,
            canonical_publication_key(execution_id, delivery.message.target),
            public_envelope_digest(public),
        ),
    )
    return {**public, PRIVATE_WIRE_FIELD: FieldCodec.encode(policy)}
