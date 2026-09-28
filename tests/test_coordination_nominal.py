"""Stored lifecycle names, graph/data ownership, and current codec paths."""

import json
from dataclasses import fields, replace
from pathlib import Path

import pytest

from agent_comms import coordination as c
from agent_comms.attempt_states import AttemptState, SettlingAttempt, SucceededAttempt
from agent_comms.assignment_states import AssignmentState, EngagedAssignment, FullPendingAssignment
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


@pytest.mark.parametrize(
    "tag,family",
    [
        ("ExecutionStatus", ExecutionState),
        ("ObligationState", ResponseState),
        ("AttemptPhase", AttemptState),
        ("ClaimDisposition", AssignmentState),
    ],
)
def test_stored_names_and_edges_match_pre_refactor_capture(tag, family):
    capture = json.loads(
        (Path(__file__).parents[1] / "evidence/s3/legacy-lifecycles.json").read_text()
    )[tag]
    assert set(family.names()) == set(capture["names"])
    assert {name: sorted(edges) for name, edges in family.transition_table().items()} == capture[
        "edges"
    ]


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
    with pytest.raises(c.IntegrityViolationError):
        SucceededAttempt.load(None, False, True)


def test_record_replacement_uses_only_nominal_state():
    record = c.ExecutionRecord(
        execution_id="e",
        origin=c.ExecutionOrigin.ACP,
        lifecycle=PendingExecution(),
        owner_thread="t",
        owner_lookup="o",
        revision=1,
        max_attempts=3,
        reason_code=None,
        created_at_ms=0,
        updated_at_ms=0,
    )
    assert {"status", "current_attempt_ordinal"}.isdisjoint(f.name for f in fields(record))
    active = replace(record, revision=2, lifecycle=ActiveExecution.load(1))
    assert active.lifecycle == ActiveExecution(1)
    assert c.execution_status_transition_allowed(record, active)
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
        assert tag.publication() == "reviewed"
        projection = ProjectedExecution(
            PendingExecution,
            c.ExecutionOrigin.WIRE,
            False,
            None,
            False,
            tag.publication(),
        )
        assert _valid_projection(
            FieldCodec.encode(AvailableRecoveryProjection("owner", 0, projection, None, None)),
            "owner",
        )
    finally:
        ResponseState.__registry__.pop("reviewed")


@pytest.mark.asyncio
async def test_execution_extension_is_stored_transitioned_and_read_over_gateway_schema(tmp_path):
    from agent_comms.coordination_store import MutationStore
    from agent_comms.execution_states import UnstartedExecution

    class PausedExecution(UnstartedExecution):
        @classmethod
        def successors(cls):
            return (PendingExecution,)

    try:
        with MutationStore(tmp_path / "coordination.sqlite3") as store:
            store.register_participant("owner", "owner", "owner", committed=True)
            store._connection.execute(
                "INSERT INTO executions(execution_id,origin,status,owner_thread,"
                "owner_lookup,revision,"
                "current_attempt_ordinal,max_attempts,reason_code,created_at_ms,updated_at_ms) "
                "VALUES ('e','acp','paused','owner','owner',1,NULL,2,NULL,1,1)"
            )
            record = store.snapshot("e").execution
            assert isinstance(record.lifecycle, PausedExecution)
            assert FieldCodec.decode(type(record), FieldCodec.encode(record)) == record
            store._connection.execute(
                "UPDATE executions SET status='pending',revision=2 WHERE execution_id='e'"
            )
            assert isinstance(store.snapshot("e").execution.lifecycle, PendingExecution)
            projection = ProjectedExecution(
                type(record.lifecycle), record.origin, False, None, False, None
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
async def test_durable_turn_records_native_phases_before_completion(tmp_path):
    from agent_comms.coordination_store import MutationStore, prepare_fence_token
    from agent_comms.durable_turn import DurableTurn
    from agent_comms.pi_events import PiEvent

    with MutationStore(tmp_path / "coordination.sqlite3") as store:
        store.register_participant("owner", "owner", "owner", committed=True)
        created = store.create_execution("e", c.ExecutionOrigin.ACP, "owner", "owner", 1).value
        pending = store.mark_pending("e", expected_revision=created.execution.revision).value
        started = store.start_attempt(
            "e",
            1,
            "owner",
            1,
            prepare_fence_token(),
            expected_execution_revision=pending.execution.revision,
            expected_pointer_revision=pending.pointer_revision,
        ).value
        progress = DurableTurn(store, started.fence, started.snapshot.pointer_revision, "input")
        samples = [
            (
                {"type": "response", "id": "native-prompt", "command": "prompt", "success": True},
                "prompt_accepted",
            ),
            ({"type": "context_committed", "inputId": "input"}, "model_running"),
            ({"type": "tool_execution_start", "toolCallId": "one"}, "tool_running"),
            ({"type": "tool_execution_start", "toolCallId": "two"}, "tool_running"),
            ({"type": "tool_execution_end", "toolCallId": "one"}, "tool_running"),
            ({"type": "tool_execution_end", "toolCallId": "two"}, "model_running"),
            ({"type": "compaction_start"}, "compaction"),
            ({"type": "compaction_end"}, "model_running"),
        ]
        for raw, expected in samples:
            await progress.dispatch(PiEvent.from_wire(raw))
            attempt = store.snapshot("e").attempt
            assert attempt.lifecycle.declared_name == expected
            assert not attempt.lifecycle.backend_done and not attempt.lifecycle.process_dead
        final = progress.finish()
        assert final.revision > started.fence.revision
        assert store.snapshot("e").attempt.lifecycle.backend_done
        assert isinstance(store.snapshot("e").attempt.lifecycle, SettlingAttempt)
        store.settle_nonpublication(
            final, expected_pointer_revision=started.snapshot.pointer_revision, success=True
        )
        assert store.snapshot("e").execution.lifecycle.completed


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
    from agent_comms.coordination_store import MutationStore

    class ReviewedResponse(ResponseState):
        @classmethod
        def successors(cls):
            return (SilentResponse,)

    try:
        with MutationStore(tmp_path / "coordination.sqlite3") as store:
            store.register_participant("p", "owner", "owner", committed=True)
            with store._transaction() as db:
                db.execute(
                    "INSERT INTO executions(execution_id,origin,status,exact_target,owner_thread,"
                    "owner_lookup,revision,current_attempt_ordinal,max_attempts,"
                    "reason_code,created_at_ms,updated_at_ms) "
                    "VALUES ('e','wire','queued','requester','owner','p',1,NULL,2,NULL,0,0)"
                )
                db.execute(
                    "INSERT INTO obligations(execution_id,exact_target,state,reason_code,"
                    "created_at_ms,"
                    "updated_at_ms,revision,receipt_message_id,receipt_seq) "
                    "VALUES ('e','requester','reviewed',NULL,0,0,1,NULL,NULL)"
                )
                db.execute(
                    "INSERT INTO wake_claims(claim_id,recipient,recipient_lookup,wire_seq,"
                    "message_id,"
                    "exact_target,audience,wake_mode,triage_verdict,disposition,resolver_version,"
                    "policy_version,accepted_at_ms,updated_at_ms,revision,execution_id) "
                    "VALUES ('c','owner','p',1,'m','requester','direct','full',NULL,'engaged',"
                    "'resolver-v1','policy-v1',0,0,1,'e')"
                )
                db.execute("INSERT INTO execution_claims VALUES ('e','c',0)")
            obligation = store.snapshot("e").obligation
            assert isinstance(obligation.lifecycle, ReviewedResponse)
            assert FieldCodec.decode(type(obligation), FieldCodec.encode(obligation)) == obligation
            projection = AvailableRecoveryProjection(
                "owner",
                0,
                ProjectedExecution(
                    QueuedExecution,
                    c.ExecutionOrigin.WIRE,
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
            store._connection.execute(
                "UPDATE obligations SET state='silent',revision=2 WHERE execution_id='e'"
            )
            assert isinstance(store.snapshot("e").obligation.lifecycle, SilentResponse)
    finally:
        ResponseState.__registry__.pop("reviewed")
