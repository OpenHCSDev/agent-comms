"""Stored lifecycle names, graph/data ownership, and current codec paths."""

import json
from agent_comms.attempt_states import SucceededAttempt
from dataclasses import fields, replace
from pathlib import Path

import pytest

from agent_comms.assignment_states import AssignmentState, EngagedAssignment, FullPendingAssignment
from agent_comms.attempt_start import AttemptStart
from agent_comms.attempt_states import SettlingAttempt, SucceededAttempt
from agent_comms.coordination_errors import IntegrityViolationError
from agent_comms.coordination_tables.assignments import MessageAudience, WakeAssignment
from agent_comms.coordination_tables.executions import ExecutionOrigin, ExecutionRecord
from agent_comms.execution_states import (
    ActiveExecution,
    ExecutionState,
    PendingExecution,
    QueuedExecution,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.obligation_states import PublishedResponse, ResponseState, SilentResponse
from agent_comms.recovery_gateway_client import _valid_projection
from agent_comms.recovery_projection import AvailableRecoveryProjection, ProjectedExecution
from agent_comms.wake_policy import FullWake


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
        origin=ExecutionOrigin.ACP,
        lifecycle=PendingExecution(),
        owner_thread="t",
        owner_lookup="o",
        revision=1,
        max_attempts=3,
        reason_code=None,
        created_at_ms=0,
        updated_at_ms=0,
    )
    assert {"status", "current_attempt_ordinal"}.isdisjoint(
        f.name for f in fields(record) if f.init
    )
    active = replace(record, revision=2, lifecycle=ActiveExecution.load(1))
    assert active.lifecycle == ActiveExecution(1)
    assert replace(active, revision=3).lifecycle == active.lifecycle


def test_gateway_uses_declared_fields_and_rejects_missing_tags_and_bool_numbers():
    value = FieldCodec.encode(AvailableRecoveryProjection("owner", 0, None, None, None))
    assert _valid_projection(value, "owner")
    for key in ("schema", "availability", "current"):
        bad = dict(value)
        del bad[key]
        assert not _valid_projection(bad, "owner")
    assert not _valid_projection(dict(value, sampledAtMs=True), "owner")
    assert not _valid_projection(dict(value, owner="other"), "owner")


def test_response_extension_decodes_transitions_and_projects_without_catalog_edits():
    class ReviewedResponse(ResponseState):
        @classmethod
        def successors(cls):
            return (SilentResponse,)

    try:
        state = FieldCodec.decode(ResponseState, {"kind": "reviewed"})
        assert state.may_become(SilentResponse())
        tag = ResponseState.decode("reviewed")
        assert tag is ReviewedResponse
        projection = ProjectedExecution(
            PendingExecution,
            ExecutionOrigin.WIRE,
            False,
            None,
            False,
            (tag,),
        )
        assert _valid_projection(
            FieldCodec.encode(AvailableRecoveryProjection("owner", 0, projection, None, None)),
            "owner",
        )
    finally:
        ResponseState.__registry__.pop("reviewed")


@pytest.mark.asyncio
async def test_execution_extension_is_stored_transitioned_and_read_over_gateway_schema(tmp_path):
    from agent_comms.coordinator import Coordination
    from agent_comms.execution_states import UnstartedExecution

    class PausedExecution(UnstartedExecution):
        @classmethod
        def successors(cls):
            return (PendingExecution,)

    try:
        with Coordination(tmp_path / "coordination.sqlite3") as store:
            store.participants.register("owner", "owner", "owner", committed=True)
            store.session._connection.execute(
                "INSERT INTO executions (execution_id,origin,lifecycle,owner_thread,owner_loo"
                "kup,revision,max_attempts,reason_code,created_at_ms,updated_at_ms) VALUES ('"
                "e','acp',json_object('kind','paused'),'owner','owner',1,2,NULL,1,1)"
            )
            record = store.snapshots.get("e").execution
            assert isinstance(record.lifecycle, PausedExecution)
            assert FieldCodec.decode(type(record), FieldCodec.encode(record)) == record
            store.session._connection.execute(
                "UPDATE executions SET lifecycle=json_set(lifecycle,'$.kind','pending'),revis"
                "ion=2 WHERE execution_id='e'"
            )
            assert isinstance(store.snapshots.get("e").execution.lifecycle, PendingExecution)
            projection = ProjectedExecution(
                type(record.lifecycle), record.origin, False, None, False, ()
            )
            result = await _through_socket(
                AvailableRecoveryProjection("owner", 1, projection, None, None)
            )
            assert result["current"]["status"] == "paused"
            assert _valid_projection(
                FieldCodec.encode(AvailableRecoveryProjection("owner", 1, projection, None, None)),
                "owner",
            )
    finally:
        ExecutionState.__registry__.pop("paused")


@pytest.mark.asyncio
async def test_durable_turn_records_native_phases_before_completion(tmp_path, monkeypatch):
    import asyncio
    import os
    import sqlite3
    from contextlib import closing

    from agent_comms.agent_events import NativePhaseChanged
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.coordinator import Coordination
    from agent_comms.durable_turn import DurableTurn
    from agent_comms.owner_fence import prepare_fence_token
    from agent_comms.pi_events import PiEvent
    from agent_comms.turn_phase import CompactionPhase, ModelWaitPhase, ToolRunningPhase

    selected = []
    original_selection = DurableTurn.handlers_for

    def observed_selection(self, event):
        selected.append(event)
        yield from original_selection(self, event)

    monkeypatch.setattr(DurableTurn, "handlers_for", observed_selection)

    with Coordination(tmp_path / "coordination.sqlite3") as store:
        store.participants.register("owner", "owner", "owner", committed=True)
        created = store.executions.create("e", ExecutionOrigin.ACP, "owner", "owner", 1).value
        pending = store.executions.mark_pending(
            "e", expected_revision=created.execution.revision
        ).value
        started = store.attempts.start(
            AttemptStart(
                "e",
                1,
                "owner",
                1,
                prepare_fence_token(),
                expected_execution_revision=pending.execution.revision,
                expected_pointer_revision=pending.pointer_revision,
            )
        ).value
        progress = DurableTurn(
            store.attempts, started.fence, started.snapshot.pointer_revision, "input"
        )
        samples = [
            (
                {"type": "response", "id": "native-prompt", "command": "prompt", "success": True},
                "prompt_accepted",
            ),
            ({"type": "context_committed", "inputId": "input"}, "model_running"),
        ]
        for raw, expected in samples:
            event = PiEvent.from_wire(raw)
            await progress.dispatch(event)
            assert sum(observation is event for observation in selected) == 1
            attempt = store.snapshots.get("e").attempt
            assert attempt.lifecycle.declared_name == expected
            assert not attempt.lifecycle.backend_done and not attempt.lifecycle.process_dead
        identity = ProcessIdentity.capture(os.getpid())
        # Native owns phase interpretation; durable projection consumes the
        # observed phase rather than independently counting tool starts/ends.
        phases = [
            (ToolRunningPhase(), "tool_running"),
            (ModelWaitPhase(), "model_running"),
            (CompactionPhase(), "compaction"),
            (ModelWaitPhase(), "model_running"),
        ]
        for phase, expected in phases:
            event = NativePhaseChanged(phase, identity)
            await progress.dispatch(event)
            assert sum(observation is event for observation in selected) == 1
            assert store.snapshots.get("e").attempt.lifecycle.declared_name == expected

        # A real SQLite blocker must not prevent the loop from releasing it.
        # Cancellation joins the owned observation, retaining its exact fence.
        with closing(sqlite3.connect(store.session.path, isolation_level=None)) as blocker:
            blocker.execute("BEGIN EXCLUSIVE")
            unhandled = PiEvent.from_wire({"type": "unhandled-observation"})
            assert await progress.dispatch(unhandled) is unhandled
            event = NativePhaseChanged(ToolRunningPhase(), identity)
            observation = asyncio.create_task(progress.dispatch(event))
            asyncio.get_running_loop().call_later(0.1, blocker.rollback)
            await asyncio.sleep(0)
            observation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observation
        assert sum(observation is event for observation in selected) == 1
        observed = store.snapshots.get("e").attempt
        assert observed.lifecycle.declared_name == "tool_running"
        assert progress.fence.revision == observed.revision
        before_finish = progress.fence.revision
        final = progress.finish()
        assert final.revision == before_finish + 1
        assert store.snapshots.get("e").attempt.lifecycle.backend_done
        assert isinstance(store.snapshots.get("e").attempt.lifecycle, SettlingAttempt)
        store.attempts.settle_nonpublication(
            final,
            expected_pointer_revision=started.snapshot.pointer_revision,
            outcome=SucceededAttempt(),
        )
        assert store.snapshots.get("e").execution.lifecycle.completed


async def _through_socket(projection):
    import asyncio
    import tempfile

    from agent_comms.recovery_gateway_client import read_gateway_projection

    with tempfile.TemporaryDirectory(prefix="s3-wire-", dir="/var/tmp") as directory:
        root = Path(directory)
        folder = root / ".recovery-viewer"
        folder.mkdir(mode=0o700)
        path = folder / "gateway.sock"

        async def reply(reader, writer):
            await reader.read()
            writer.write((json.dumps(FieldCodec.encode(projection)) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(reply, path=path)
        path.chmod(0o600)
        async with server:
            return await read_gateway_projection(path, "owner")


@pytest.mark.asyncio
async def test_new_response_state_roundtrips_real_store_and_socket(tmp_path):
    from agent_comms.coordinator import Coordination

    class ReviewedResponse(ResponseState):
        @classmethod
        def successors(cls):
            return (SilentResponse,)

    try:
        with Coordination(tmp_path / "coordination.sqlite3") as store:
            store.participants.register("p", "owner", "owner", committed=True)
            with store.session.transaction() as db:
                db.execute(
                    "INSERT INTO executions (execution_id,origin,lifecycle,exact_target,owner_thr"
                    "ead,owner_lookup,revision,max_attempts,reason_code,created_at_ms,updated_at_"
                    "ms) VALUES ('e','wire',json_object('kind','queued'),'requester','owner','p',"
                    "1,2,NULL,0,0)"
                )
                db.execute(
                    "INSERT INTO obligations (execution_id,exact_target,lifecycle,reason_code,cre"
                    "ated_at_ms,updated_at_ms,revision) VALUES ('e','requester',json_object('kind"
                    "','reviewed'),NULL,0,0,1)"
                )
                db.execute(
                    "INSERT INTO wake_claims (assignment_id,recipient,recipient_lookup,wire_seq,m"
                    "essage_id,lifecycle,audience,resolver_version,policy_version,accepted_at_ms,"
                    "updated_at_ms,revision) VALUES ('c','owner','p',1,'m',json_object('kind','en"
                    "gaged','decision',json_object('kind','full','exact_target','requester','exec"
                    "ution_id','e')),'direct','resolver-v1','policy-v1',0,0,1)"
                )
                db.execute("INSERT INTO execution_claims VALUES ('e','c',0)")
            obligation = store.snapshots.get("e").obligation
            assert isinstance(obligation.lifecycle, ReviewedResponse)
            assert FieldCodec.decode(type(obligation), FieldCodec.encode(obligation)) == obligation
            projection = AvailableRecoveryProjection(
                "owner",
                0,
                ProjectedExecution(
                    QueuedExecution,
                    ExecutionOrigin.WIRE,
                    False,
                    None,
                    False,
                    obligation.lifecycle.publication(),
                ),
                None,
                None,
            )
            result = await _through_socket(projection)
            assert result["current"]["publication"] == "reviewed"
            store.session._connection.execute(
                "UPDATE obligations SET lifecycle=json_object('kind','silent'),revision=2 WHE"
                "RE execution_id='e'"
            )
            assert isinstance(store.snapshots.get("e").obligation.lifecycle, SilentResponse)
    finally:
        ResponseState.__registry__.pop("reviewed")


def test_assignment_extension_derives_sql_projection_and_transitions(tmp_path):
    from agent_comms.assignment_states import FailedAssignment
    from agent_comms.coordinator import Coordination

    class AwaitingAssignment(FullPendingAssignment):
        pass

    try:
        with Coordination(tmp_path / "coordination.sqlite3") as store:
            store.participants.register("owner", "owner", "owner", committed=True)
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
            with store.session.transaction() as db:
                assignment.insert(db)
            assert store.assignments.get("assignment") == assignment
            assert store.assignments.get("assignment").wake_mode == "full"
            settled = store.assignments.transition_preengagement(
                "assignment",
                FailedAssignment,
                expected_revision=1,
            ).value
            assert settled.lifecycle.failed and settled.lifecycle.mode == assignment.lifecycle.mode
            assert store.assignments.get("assignment") == settled
    finally:
        AssignmentState.__registry__.pop("awaiting")


def test_original_target_and_receipt_legality_is_owned_by_current_variants():
    """The three original checks with retired diagnostics now have typed owners."""
    # Unstarted assignments cannot carry a frozen response target at all.
    unstarted = FieldCodec.encode(FullPendingAssignment())
    assert FieldCodec.decode(AssignmentState, unstarted) == FullPendingAssignment()
    with pytest.raises(ValueError, match="Unknown fields"):
        FieldCodec.decode(AssignmentState, dict(unstarted, exact_target="requester"))

    # An engagement cannot omit either member of its exact execution binding.
    engaged = FieldCodec.encode(EngagedAssignment.load(FullWake(), None, "e", "requester"))
    for field in ("exact_target", "execution_id"):
        damaged = {**engaged, "decision": dict(engaged["decision"])}
        del damaged["decision"][field]
        with pytest.raises((TypeError, ValueError)):
            FieldCodec.decode(AssignmentState, damaged)

    # A publication has a complete receipt; non-publication variants have none.
    published = FieldCodec.encode(PublishedResponse("message", 1))
    for field in ("message_id", "seq"):
        damaged = dict(published)
        del damaged[field]
        with pytest.raises((TypeError, ValueError)):
            FieldCodec.decode(ResponseState, damaged)
    with pytest.raises(ValueError, match="Unknown fields"):
        FieldCodec.decode(ResponseState, {**FieldCodec.encode(SilentResponse()), "seq": 1})
