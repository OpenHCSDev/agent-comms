"""Real bus/SQL reply delivery, awareness and missing-native-proof boundaries."""

from pathlib import Path

import pytest

from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_foreground import _accept_visible_deliveries
from agent_comms.coordination_cohort import sealed_cohort_assignments
from agent_comms.coordination_response import prepare_fenced_response, publish_fenced_response
from agent_comms.private_bus_checkpoint import addressed_source_pointers_unlocked
from agent_comms.proven_source_coverage import SourceCoverage
from agent_comms.threads import Thread
from test_coordination_response import _ready


@pytest.mark.parametrize("direct", [False, True])
def test_reply_has_frozen_awareness_and_selected_native_barrier(tmp_path: Path, direct: bool):
    case = _ready(tmp_path, direct=direct)
    try:
        # A subscriber arriving after the original is not a historical recipient.
        case.comms.registry.declare(Thread("late", frozenset({"team"}), worktree=str(tmp_path)))
        sender = case.comms.registry.require("sender")
        lookup = stable_thread_lookup(sender.created_at)
        intent = prepare_fenced_response(
            case.store,
            case.bus,
            case.fence,
            "Here is the answer @sender",
            owner_witness=case.witness,
        ).value
        publish_fenced_response(case.store, case.bus, case.fence, owner_witness=case.witness)
        response = case.bus.log.read_keyed_response(intent)
        delivery = case.bus.log.read_delivery_cohort(case.root_id, response.seq)
        assert delivery.audience.canonical_members == {"sender"}
        assert delivery.decisions[0].wake_mode.triage
        coverage = SourceCoverage(
            case.bus, case.store, wire_root_id=case.root_id, recipient_lookup=lookup
        )
        assert coverage.read().blocked_seq == response.seq
        cursor = _accept_visible_deliveries(
            case.bus, case.root_id, case.store, lookup, 0, owner_name="sender"
        )
        assert cursor == response.seq
        assignments = sealed_cohort_assignments(case.store, lookup)
        assert [a.wire_seq for a in assignments] == [response.seq]
        # Acceptance does not pretend the model saw the reply.
        assert coverage.read().blocked_seq == response.seq
        assert coverage.read().injected_source_seqs == ()
        with case.bus.log.locked():
            marker = case.bus.log._private_marker_unlocked()
            pointers = addressed_source_pointers_unlocked(case.bus.log, marker, lookup)
        assert [p.seq for p in pointers] == [response.seq]
        assert str(response.seq) in case.bus.awareness_prompt(sender)
        # Optional scheduling hints consume the same canonical reply declaration.
        from agent_comms.wake_candidate_index import WakeCandidateIndex

        candidates = WakeCandidateIndex(case.bus)
        assert candidates.maintain(rebuild=True)
        page = candidates.page(
            root_id=case.root_id, recipient_lookup=lookup, after_seq=0,
            required_through_seq=response.seq,
        )
        assert [entry.source_seq for entry in page.entries] == [response.seq]
        assert page.entries[0].wake_mode == "bounded_triage"
        # Repeating the publisher/recipient poll does not duplicate the row or K.
        publish_fenced_response(case.store, case.bus, case.fence, owner_witness=case.witness)
        _accept_visible_deliveries(
            case.bus, case.root_id, case.store, lookup, 0, owner_name="sender"
        )
        assert len(case.bus.log.full_history()) == 2
        assert sealed_cohort_assignments(case.store, lookup) == assignments
    finally:
        case.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("wire_root_id", "0" * 32),
        ("envelope_digest", "0" * 64),
        ("publication_key", "publication:v1:exec:wrong-route"),
        ("execution_id", 3),
        ("publication_key", None),
        ("unexpected", "not permitted"),
    ],
)
@pytest.mark.parametrize("reader", ["full", "certified"])
def test_canonical_response_reader_rejects_invalid_receipt(tmp_path, field, value, reader):
    import json

    from agent_comms.bus_publication import CommittedDelivery
    from agent_comms.errors import RelationViolationError
    from agent_comms.private_bus_checkpoint import (
        certified_delivery_page_unlocked,
        install_private_bus_checkpoint,
    )

    case = _ready(tmp_path, direct=True)
    try:
        prepare_fenced_response(
            case.store, case.bus, case.fence, "answer", owner_witness=case.witness
        )
        publish_fenced_response(case.store, case.bus, case.fence, owner_witness=case.witness)
        marker = case.bus.log._private_marker_unlocked()
        rows = [json.loads(line) for line in case.bus.log.path.read_bytes().splitlines()]
        rows[-1]["_agent_comms_private_v1"]["response"][field] = value
        # The semantic decoder shared by both readers rejects each invalid receipt.
        with pytest.raises((ValueError, TypeError)):
            CommittedDelivery.from_wire(rows[-1], case.root_id)
        case.bus.log.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        if reader == "certified":
            # A real certified index refuses tampered bytes at its earlier prefix
            # fence. Do not forge a certificate just to reach the decoder again.
            checkpoint = case.comms.root / "private_bus_checkpoint.sqlite3"
            saved_index = checkpoint.read_bytes()
            sender = case.comms.registry.require("sender")
            with pytest.raises(RelationViolationError):
                certified_delivery_page_unlocked(
                    case.bus.log, marker, stable_thread_lookup(sender.created_at)
                )
            assert checkpoint.read_bytes() == saved_index
            return
        # Explicit cold certification must reject a corrupt candidate rather
        # than trusting a stale index or accepting a merely well-shaped receipt.
        (case.comms.root / "private_bus_checkpoint.sqlite3").unlink()
        marker.checkpoint_version = None
        marker.checkpoint_seal = None
        case.bus.log.write_metadata_unlocked(marker)
        with pytest.raises((ValueError, RelationViolationError)):
            install_private_bus_checkpoint(case.bus.log)
    finally:
        case.close()


def test_duplicate_valid_response_key_denied_by_cold_reader(tmp_path):
    import json
    from dataclasses import replace

    from agent_comms.errors import RelationViolationError
    from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
    from agent_comms.response_conversation import ResponseConversation

    case = _ready(tmp_path, direct=True)
    try:
        intent = prepare_fenced_response(
            case.store, case.bus, case.fence, "answer", owner_witness=case.witness
        ).value
        publish_fenced_response(case.store, case.bus, case.fence, owner_witness=case.witness)
        response = case.bus.log.read_keyed_response(intent)
        with case.bus.log.locked():
            conversation = ResponseConversation.capture(case.bus, case.store.snapshots.get("exec"))
            duplicate = conversation.record(
                case.root_id, replace(response, seq=response.seq + 1), intent
            )
            marker = case.bus.log._private_marker_unlocked()
        with case.bus.log.path.open("a") as stream:
            stream.write(json.dumps(duplicate) + "\n")
        (case.comms.root / "private_bus_checkpoint.sqlite3").unlink()
        marker.last_seq = response.seq + 1
        marker.checkpoint_version = None
        marker.checkpoint_seal = None
        case.bus.log.write_metadata_unlocked(marker)
        with pytest.raises(RelationViolationError, match="Malformed public bus row"):
            install_private_bus_checkpoint(case.bus.log)
    finally:
        case.close()


@pytest.mark.parametrize("kind", [None, "unknown_delivery"])
def test_delivery_requires_declaration_tag_not_field_shape(tmp_path, kind):
    import json

    from agent_comms.bus_publication import PRIVATE_WIRE_FIELD, CommittedDelivery

    case = _ready(tmp_path, direct=True)
    try:
        prepare_fenced_response(
            case.store, case.bus, case.fence, "answer", owner_witness=case.witness
        )
        publish_fenced_response(case.store, case.bus, case.fence, owner_witness=case.witness)
        record = json.loads(case.bus.log.path.read_bytes().splitlines()[-1])
        private = record[PRIVATE_WIRE_FIELD]
        if kind is None:
            del private["kind"]
        else:
            private["kind"] = kind
        with pytest.raises(ValueError):
            CommittedDelivery.from_wire(record, case.root_id)
    finally:
        case.close()
