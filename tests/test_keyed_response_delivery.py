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
        case.comms.threads.register(Thread("late", frozenset({"team"}), worktree=str(tmp_path)))
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
        # Repeating the publisher/recipient poll does not duplicate the row or K.
        publish_fenced_response(case.store, case.bus, case.fence, owner_witness=case.witness)
        _accept_visible_deliveries(
            case.bus, case.root_id, case.store, lookup, 0, owner_name="sender"
        )
        assert len(case.bus.log.full_history()) == 2
        assert sealed_cohort_assignments(case.store, lookup) == assignments
    finally:
        case.close()
