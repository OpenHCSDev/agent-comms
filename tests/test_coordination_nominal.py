"""Compatibility graphs and state data ownership, independent of the new implementation."""

import json
from dataclasses import fields, replace
from pathlib import Path

import pytest

from agent_comms import coordination as c
from agent_comms.attempt_states import AttemptState, SucceededAttempt
from agent_comms.claim_states import ClaimState, EngagedClaim, FullPendingClaim
from agent_comms.execution_states import ActiveExecution, ExecutionState, PendingExecution
from agent_comms.field_codec import FieldCodec
from agent_comms.obligation_states import PublishedResponse, ResponseState, SilentResponse
from agent_comms.recovery_gateway_client import _valid_projection
from agent_comms.recovery_projection import AvailableRecoveryProjection, ProjectedExecution


@pytest.mark.parametrize(
    "tag,family",
    [
        (c.ExecutionStatus, ExecutionState),
        (c.ObligationState, ResponseState),
        (c.AttemptPhase, AttemptState),
        (c.ClaimDisposition, ClaimState),
    ],
)
def test_stored_names_and_edges_match_pre_refactor_capture(tag, family):
    capture = json.loads(
        (Path(__file__).parents[1] / "evidence/s3/legacy-lifecycles.json").read_text()
    )[tag.__name__]
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
        FullPendingClaim(execution_id="other")
    with pytest.raises(TypeError):
        EngagedClaim()
    with pytest.raises(TypeError):
        SucceededAttempt(lease_expires_at_ms=100)
    with pytest.raises(c.IntegrityViolationError):
        SucceededAttempt.load(None, False, True)


def test_record_keeps_only_nominal_state_and_legacy_replace_decodes_at_boundary():
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
    active = replace(record, status=c.ExecutionStatus.ACTIVE, current_attempt_ordinal=1, revision=2)
    assert active.lifecycle == ActiveExecution(1)
    assert c.execution_status_transition_allowed(record, active)
    assert replace(active, revision=3).lifecycle == active.lifecycle


def test_gateway_uses_declared_fields_and_rejects_missing_tags_and_bool_numbers():
    value = AvailableRecoveryProjection("owner", 0, None, None, None).to_primitive()
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
        tag = c.ObligationState("reviewed")
        assert tag.declaration.publication() == "reviewed"
        projection = ProjectedExecution(
            c.ExecutionStatus.PENDING,
            c.ExecutionOrigin.WIRE,
            False,
            None,
            False,
            tag.declaration.publication(),
        )
        assert _valid_projection(
            AvailableRecoveryProjection("owner", 0, projection, None, None).to_primitive(), "owner"
        )
    finally:
        ResponseState.__registry__.pop("reviewed")
