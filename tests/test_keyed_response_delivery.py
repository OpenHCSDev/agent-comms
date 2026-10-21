"""Real bus/SQL reply delivery, awareness and missing-native-proof boundaries."""

from agent_comms.wake_candidate_index import RebuildIndex
from agent_comms.wire_record import WireRecord

from pathlib import Path

import pytest
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.bus_source_page import AddressedPage

from agent_comms.cohort_foreground import _accept_visible_deliveries
from agent_comms.coordination_cohort import sealed_cohort_assignments
from agent_comms.coordination_response import prepare_fenced_response, publish_fenced_response
from agent_comms.private_bus_checkpoint import addressed_source_pointers_unlocked
from agent_comms.proven_source_coverage import SourceCoverage
from agent_comms.wake_policy import BoundedTriageWake
from test_coordination_response import _ready
from bus_history import read_history


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
async def test_reply_has_frozen_awareness_and_selected_native_barrier(tmp_path: Path, direct: bool, *, source_reads):
    case = _ready(tmp_path, direct=direct)
    try:
        # A subscriber arriving after the original is not a historical recipient.
        case.comms.registry.declare(case.comms.registry.new_thread('late', frozenset({'team'}), worktree=str(tmp_path)))
        sender = case.comms.registry.require("sender")
        lookup = RecordedThreadIncarnation("fixture", sender.created_at).lookup
        intent = prepare_fenced_response(
            case.store,
            case.bus,
            case.fence,
            "Here is the answer @sender",
            exact_target=case.reply_target, owner_witness=case.witness,
        ).value
        publish_fenced_response(case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness)
        response = case.bus.log.read_keyed_response(intent)
        delivery = case.bus.log.read_delivery_cohort(case.root_id, response.seq)
        assert delivery.audience.canonical_members == {"sender"}
        assert delivery.decisions[0].wake_mode.triage
        coverage = SourceCoverage(
            case.bus, case.store, wire_root_id=case.root_id, recipient_lookup=lookup
        )
        assert coverage.read(source_reads=source_reads).blocked_seq == response.seq
        cursor = await _accept_visible_deliveries(
            case.bus, case.root_id, case.store.session.path, lookup, 0, owner_name="sender"
        )
        assert cursor == response.seq
        assignments = sealed_cohort_assignments(case.store, lookup)
        assert [a.wire_seq for a in assignments] == [response.seq]
        # Acceptance does not pretend the model saw the reply.
        assert coverage.read(source_reads=source_reads).blocked_seq == response.seq
        assert coverage.read(source_reads=source_reads).injected_source_seqs == ()
        with case.bus.log.locked():
            marker = case.bus.log.private_marker_unlocked()
            pointers = addressed_source_pointers_unlocked(case.bus.log, marker, lookup)
        assert [p.seq for p in pointers] == [response.seq]
        assert str(response.seq) in "".join(segment.text() for segment in case.bus.awareness_segments(sender, ()))
        # Optional scheduling hints consume the same canonical reply declaration.
        from agent_comms.wake_candidate_index import WakeCandidateIndex

        candidates = WakeCandidateIndex(case.bus.log)
        assert candidates.maintain(start=RebuildIndex())
        page = candidates.page(
            root_id=case.root_id, recipient_lookup=lookup, after_seq=0,
            required_through_seq=response.seq,
        )
        assert [entry.source_seq for entry in page.entries] == [response.seq]
        assert page.entries[0].wake_mode is BoundedTriageWake
        # Repeating the publisher/recipient poll does not duplicate the row or K.
        publish_fenced_response(case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness)
        await _accept_visible_deliveries(
            case.bus, case.root_id, case.store.session.path, lookup, 0, owner_name="sender"
        )
        assert len(read_history(case.comms)) == 2
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
@pytest.mark.parametrize("reader", ["full", "checked", "unchecked"])
def test_canonical_response_reader_rejects_invalid_receipt(tmp_path, field, value, reader):
    import json

    from agent_comms.errors import RelationViolationError
    from agent_comms.private_bus_checkpoint import (
        install_private_bus_checkpoint,
    )

    case = _ready(tmp_path, direct=True)
    try:
        prepare_fenced_response(
            case.store, case.bus, case.fence, "answer", exact_target=case.reply_target, owner_witness=case.witness
        )
        publish_fenced_response(case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness)
        marker = case.bus.log.private_marker_unlocked()
        rows = [json.loads(line) for line in case.bus.log.path.read_bytes().splitlines()]
        rows[-1]["_agent_comms_private_v1"]["response"][field] = value
        # The semantic decoder shared by both readers rejects each invalid acknowledgement.
        with pytest.raises((ValueError, TypeError)):
            WireRecord.from_wire(rows[-1], case.root_id)
        case.bus.log.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        if reader == "checked":
            # A real checked index refuses tampered bytes at its earlier prefix
            # fence. Do not forge a prefix record just to reach the decoder again.
            checkpoint = case.comms.root / "private_bus_checkpoint.sqlite3"
            saved_index = checkpoint.read_bytes()
            sender = case.comms.registry.require("sender")
            with pytest.raises(RelationViolationError):
                with case.bus.log.checked_read() as source:
                    source.addressed_page(case.bus.log, AddressedPage.capture(
                        lookup=RecordedThreadIncarnation("fixture", sender.created_at).lookup))
            assert checkpoint.read_bytes() == saved_index
            return
        # Explicit cold certification must reject a corrupt candidate rather
        # than trusting a stale index or accepting a merely well-shaped acknowledgement.
        (case.comms.root / "private_bus_checkpoint.sqlite3").unlink()
        marker.unseal()
        case.bus.log.write_metadata_unlocked(marker)
        with pytest.raises((ValueError, RelationViolationError)):
            if reader == "unchecked":
                read_history(case.comms)
            else:
                install_private_bus_checkpoint(case.bus.log)
    finally:
        case.close()


@pytest.mark.parametrize("kind", [None, "unknown_delivery"])
def test_delivery_requires_declaration_tag_not_field_shape(tmp_path, kind):
    import json

    from agent_comms.bus_publication import PRIVATE_WIRE_FIELD

    case = _ready(tmp_path, direct=True)
    try:
        prepare_fenced_response(
            case.store, case.bus, case.fence, "answer", exact_target=case.reply_target, owner_witness=case.witness
        )
        publish_fenced_response(case.store, case.bus, case.fence, exact_target=case.reply_target, owner_witness=case.witness)
        record = json.loads(case.bus.log.path.read_bytes().splitlines()[-1])
        private = record[PRIVATE_WIRE_FIELD]
        if kind is None:
            del private["kind"]
        else:
            private["kind"] = kind
        with pytest.raises(ValueError):
            WireRecord.from_wire(record, case.root_id)
    finally:
        case.close()


def test_new_policy_declaration_uses_full_and_checked_record_readers(tmp_path):
    import json

    from agent_comms.bus_publication import PRIVATE_WIRE_FIELD
    from agent_comms.delivery_policy import DeliveryPolicy, InitialDeliveryPolicy
    from agent_comms.field_codec import FieldCodec
    from agent_comms.private_bus_checkpoint import (
        install_private_bus_checkpoint,
    )

    class ExtendedDeliveryPolicy(InitialDeliveryPolicy):
        pass

    case = _ready(tmp_path, direct=True)
    try:
        # Acquire the existing checked writer before replacing this disposable
        # source. Bootstrap cannot acquire a missing checkpoint as a live bus.
        with case.bus.log.locked():
            marker = case.bus.log.private_marker_unlocked()
            raw = json.loads(case.bus.log.path.read_bytes().splitlines()[0])
            policy = FieldCodec.decode(DeliveryPolicy, raw[PRIVATE_WIRE_FIELD])
            raw[PRIVATE_WIRE_FIELD] = FieldCodec.encode(
                ExtendedDeliveryPolicy(version=policy.version, initial=policy.initial)
            )
            (case.comms.root / "private_bus_checkpoint.sqlite3").unlink()
            marker.unseal()
            case.bus.log.write_metadata_unlocked(marker)
            case.bus.log.path.write_text(json.dumps(raw) + "\n")
            install_private_bus_checkpoint(case.bus.log, _bus_locked=True)
        public = read_history(case.comms)
        assert len(public) == 1
        with case.bus.log.checked_read() as source:
            marker = case.bus.log.private_marker_unlocked()
            full = tuple(case.bus.log.verified_records_unlocked(marker))
            assert full[0].message == public[0]
            delivery, = full[0].deliveries()
            recipient = delivery.audience.recipients[0].recipient_lookup
            _, checked, more = source.addressed_page(
                case.bus.log, AddressedPage.capture(lookup=recipient))
            assert tuple(checked) == (delivery,)
            assert not more
    finally:
        case.close()


def test_duplicate_json_key_is_typed_and_never_enters_unchecked_read(tmp_path):
    import json

    from agent_comms.errors import StoredHistoryUnreadable
    from agent_comms.field_codec import MalformedRecord

    case = _ready(tmp_path, direct=True)
    try:
        marker = case.bus.log.private_marker_unlocked()
        row = json.loads(case.bus.log.path.read_bytes().splitlines()[0])
        (case.comms.root / "private_bus_checkpoint.sqlite3").unlink()
        marker.unseal()
        case.bus.log.write_metadata_unlocked(marker)
        raw = '{"seq":' + str(row["seq"]) + ',' + json.dumps(row)[1:] + '\n'
        case.bus.log.path.write_text(raw)
        before = case.bus.log.path.read_bytes()
        with pytest.raises(StoredHistoryUnreadable) as raised:
            list(case.bus.log.verified_records_unlocked(marker))
        assert type(raised.value.__cause__) is StoredHistoryUnreadable
        assert type(raised.value.__cause__.__cause__) is MalformedRecord
        assert case.bus.log.path.read_bytes() == before
    finally:
        case.close()
