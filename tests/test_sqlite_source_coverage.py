"""SQLite delivery prefixes preserve native gaps without a file checkpoint."""

import pytest

from agent_comms.bus import Bus, BusRecord, PrivateRecord, PublicRecord
from agent_comms.bus_source_page import CoveragePage
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination
from agent_comms.cursor_owner import UnstartedCursor
from agent_comms.messages import InfoKind, Message
from agent_comms.proven_source_coverage import SourceCoverage, SourcePrefix
from agent_comms.publisher import ResponseWrites
from test_bus import record_from


@pytest.fixture
def coverage(tmp_path):
    Bus.install(Bus.path_in(tmp_path))
    comms = Comms(tmp_path, ResponseWrites)
    with Coordination(str(CoordinationStore.path_in(tmp_path))) as store:
        store.install_private_runtime()
        yield SourceCoverage(comms.bus, store, wire_root_id="a" * 32,
                             recipient_lookup="b" * 32)


@pytest.mark.parametrize("declaration", BusRecord.members_with(BusRecord))
def test_native_coverage_uses_delivery_membership_and_keeps_unproven_barrier(
    coverage, declaration, source_reads,
):
    record = coverage.bus.bus.append(lambda seq, db: record_from(
        declaration, Message("author", "worker", "source", InfoKind, seq=seq)))
    result = coverage.read(source_reads=source_reads)
    assert result.blocked_seq == next((row.seq for row in record.deliveries()), None)
    assert result.covered_seq == (0 if record.deliveries() else record.seq)
    assert result.source_prefix.through_seq == record.seq
    assert result.inputs == ()


def test_unstarted_coverage_needs_neither_sender_claim_nor_activation(coverage, source_reads):
    result = coverage.since(UnstartedCursor(), source_reads=source_reads)
    assert result.covered_seq == 0
    assert result.blocked_seq is None
    assert result.inputs == ()
    result.source_prefix.require_start(0)
    with pytest.raises(IdentityConflict):
        result.source_prefix.require_start(1)


def test_captured_prefix_excludes_new_input_without_rejecting_append(coverage, source_reads):
    bus = coverage.bus.bus
    public = bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "worker", "history", InfoKind, seq=seq)))
    prefix = SourcePrefix.capture(bus, 0)
    pending = bus.append(lambda seq, db: record_from(
        PrivateRecord, Message("author", "worker", "new input", InfoKind, seq=seq)))
    prefix.require_current(bus)
    result = coverage._read(
        CoveragePage.capture(lookup=coverage.recipient_lookup), prefix, source_reads)
    assert result.covered_seq == public.seq
    assert result.blocked_seq is None
    assert result.inputs == ()
    assert coverage.read(source_reads=source_reads).blocked_seq == pending.seq


def test_prefix_rejects_replaced_database_with_the_same_sequence(coverage, tmp_path):
    bus = coverage.bus.bus
    bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "worker", "history", InfoKind, seq=seq)))
    prefix = SourcePrefix.capture(bus, 0)
    replacement = tmp_path / "replacement.sqlite3"
    bus.backup(replacement)
    replacement.replace(bus.path)
    with pytest.raises(IdentityConflict):
        prefix.require_current(bus)
