"""Stored lifecycle names, graph/data ownership, and current codec paths."""

from agent_comms.lifecycle_capability import Failed
from dataclasses import fields, replace

import pytest

from agent_comms.reason_codes import NoReasonCode
from agent_comms.assignment_states import AssignmentState, EngagedAssignment, FullPendingAssignment
from agent_comms.attempt_states import SucceededAttempt
from agent_comms.coordination_errors import IntegrityViolationError
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.executions import ExecutionProvenance, ExecutionRecord, WireOrigin, AcpOrigin
from agent_comms.execution_states import (
    ActiveExecution,
    ExecutionState,
    PendingExecution,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.obligation_states import PublishedResponse, ResponseState, SilentResponse
from agent_comms.wake_policy import FullWake, MessageAudience


def test_state_data_cannot_be_attached_to_wrong_variant():
    with pytest.raises(TypeError):
        PendingExecution(1)
    with pytest.raises(TypeError):
        ActiveExecution()
    with pytest.raises(TypeError):
        SilentResponse("message", 1)
    with pytest.raises(TypeError):
        PublishedResponse()
    with pytest.raises(TypeError):
        FullPendingAssignment(execution_id="other")
    with pytest.raises(TypeError):
        EngagedAssignment()
    with pytest.raises(TypeError):
        SucceededAttempt(lease_expires_at_ms=100)
    with pytest.raises(IntegrityViolationError):
        SucceededAttempt.load(None, False, True)


def test_record_replacement_uses_only_nominal_state():
    record = ExecutionRecord(
        execution_id="e",
        origin=AcpOrigin,
        lifecycle=PendingExecution(),
        owner_lookup="o",
        revision=1,
        max_attempts=3,
        reason_code=NoReasonCode(),
        created_at_ms=0,
        updated_at_ms=0,
    )
    assert {"status", "current_attempt_ordinal"}.isdisjoint(
        f.name for f in fields(record) if f.init
    )
    active = replace(record, revision=2, lifecycle=ActiveExecution.load(1))
    assert active.lifecycle == ActiveExecution(1)
    assert replace(active, revision=3).lifecycle == active.lifecycle


def test_response_extension_decodes_transitions_and_projects_without_catalog_edits():
    class ReviewedResponse(ResponseState):
        @classmethod
        def successors(cls):
            return (SilentResponse,)

    try:
        state = FieldCodec.decode(ResponseState, {"kind": "reviewed"})
        assert state.may_become(SilentResponse())
        assert ResponseState.member_named("reviewed") is ReviewedResponse
    finally:
        ResponseState.__registry__.pop("reviewed")


def test_execution_extension_is_stored_and_transitioned(tmp_path):
    from agent_comms.coordinator import Coordination
    from agent_comms.execution_states import UnstartedExecution

    class PausedExecution(UnstartedExecution):
        @classmethod
        def successors(cls):
            return (PendingExecution,)

    try:
        with Coordination(tmp_path / "coordination.sqlite3") as store:
            store.participants.register("owner", committed=True)
            store.session._connection.execute(
                "INSERT INTO executions (execution_id,origin,lifecycle,owner_loo"
                "kup,revision,max_attempts,reason_code,created_at_ms,updated_at_ms) VALUES ('"
                "e','acp',json_object('kind','paused'),'owner',1,2,NULL,1,1)"
            )
            record = store.get("e").execution
            assert isinstance(record.lifecycle, PausedExecution)
            assert FieldCodec.decode(type(record), FieldCodec.encode(record)) == record
            store.session._connection.execute(
                "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','pending'),revis"
                "ion=2 WHERE execution_id='e'"
            )
            assert isinstance(store.get("e").execution.lifecycle, PendingExecution)
    finally:
        ExecutionState.__registry__.pop("paused")


def test_new_response_state_roundtrips_real_store(tmp_path):
    from agent_comms.coordinator import Coordination
    from agent_comms.coordination_tables.assignments import ExecutionAssignmentLink
    from agent_comms.coordination_tables.responses import ResponseObligation
    from agent_comms.execution_states import QueuedExecution

    class ReviewedResponse(ResponseState):
        @classmethod
        def successors(cls):
            return (SilentResponse,)

    try:
        with Coordination(tmp_path / "coordination.sqlite3") as store:
            store.participants.register("p", committed=True)
            with store.session.writing() as db:
                ExecutionRecord(
                    execution_id="e", origin=WireOrigin,
                    lifecycle=QueuedExecution(), owner_lookup="p",
                    revision=1, max_attempts=2, reason_code=NoReasonCode(),
                    created_at_ms=0, updated_at_ms=0,
                ).insert(db)
                WakeAssignment(
                    assignment_id="c", recipient="owner", recipient_lookup="p",
                    wire_seq=1, message_id="m", audience=MessageAudience.DIRECT,
                    lifecycle=EngagedAssignment.build(FullWake(), "e", "requester"),
                    accepted_at_ms=0, updated_at_ms=0,
                ).insert(db)
                ResponseObligation(
                    execution_id="e", exact_target="requester", lifecycle=ReviewedResponse(),
                    reason_code=NoReasonCode(), created_at_ms=0, updated_at_ms=0, revision=1,
                ).insert(db)
                ExecutionAssignmentLink("e", "c", 0).insert(db)
            obligation, = store.get("e").obligations
            assert isinstance(obligation.lifecycle, ReviewedResponse)
            assert FieldCodec.decode(type(obligation), FieldCodec.encode(obligation)) == obligation
            ResponseObligation.update(
                store.session._connection, where="execution_id=?", parameters=("e",),
                lifecycle=SilentResponse(), revision=2,
            )
            settled, = store.get("e").obligations
            assert isinstance(settled.lifecycle, SilentResponse)
    finally:
        ResponseState.__registry__.pop("reviewed")


def test_assignment_extension_derives_sql_projection_and_transitions(tmp_path):
    from agent_comms.assignment_states import FailedAssignment
    from agent_comms.coordinator import Coordination

    class AwaitingAssignment(FullPendingAssignment):
        pass

    try:
        with Coordination(tmp_path / "coordination.sqlite3") as store:
            store.participants.register("owner", committed=True)
            assignment = WakeAssignment(
                assignment_id="assignment",
                recipient="owner",
                recipient_lookup="owner",
                wire_seq=1,
                message_id="message",
                audience=MessageAudience.DIRECT,
                lifecycle=AwaitingAssignment(),
                accepted_at_ms=0,
                updated_at_ms=0,
            )
            with store.session.writing() as db:
                assignment.insert(db)
            assert store.assignments.get("assignment") == assignment
            with store.session.read():
                assert WakeAssignment.count(store.session._connection, wake_mode="full") == 1
            settled = store.assignments.transition_preengagement(
                "assignment",
                FailedAssignment,
                expected_revision=1,
            ).value
            settled.lifecycle.require(Failed, ValueError("failure required"))
            assert settled.lifecycle.mode == assignment.lifecycle.mode
            assert store.assignments.get("assignment") == settled
    finally:
        AssignmentState.__registry__.pop("awaiting")


def test_original_target_and_receipt_legality_is_owned_by_current_variants():
    """The three original checks with retired diagnostics now have typed owners."""
    # Unstarted assignments cannot carry a frozen response target at all.
    unstarted = FieldCodec.encode(FullPendingAssignment())
    assert FieldCodec.decode(AssignmentState, unstarted) == FullPendingAssignment()
    with pytest.raises(ValueError):
        FieldCodec.decode(AssignmentState, dict(unstarted, exact_target="requester"))

    # An engagement cannot omit either member of its exact execution binding.
    engaged = FieldCodec.encode(EngagedAssignment.load(FullWake(), None, "e", "requester"))
    for field in ("exact_target", "execution_id"):
        damaged = {**engaged, "decision": dict(engaged["decision"])}
        del damaged["decision"][field]
        with pytest.raises((TypeError, ValueError)):
            FieldCodec.decode(AssignmentState, damaged)

    # A publication has a complete acknowledgement; non-publication variants have none.
    published = FieldCodec.encode(PublishedResponse("message", 1))
    for field in ("message_id", "seq"):
        damaged = dict(published)
        del damaged[field]
        with pytest.raises((TypeError, ValueError)):
            FieldCodec.decode(ResponseState, damaged)
    with pytest.raises(ValueError):
        FieldCodec.decode(ResponseState, {**FieldCodec.encode(SilentResponse()), "seq": 1})
