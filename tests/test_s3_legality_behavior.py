"""Original S3 predicates executed through current families and real SQLite snapshots."""

from dataclasses import replace
from itertools import product

import pytest

from agent_comms.coordination_tables.executions import NoPointedAttempt, NoPointedExecution
from agent_comms.assignment_states import AssignmentState, BoundAssignment
from agent_comms.attempt_states import AttemptState
from agent_comms.coordination_errors import IntegrityViolationError
from agent_comms.coordination_snapshot import RecoverySnapshot
from agent_comms.coordination_tables.attempts import ReplayAssessments, ReplayFact
from agent_comms.coordination_tables.executions import ExecutionOrigin
from agent_comms.coordination_tables.publications import (
    PublicationIntents,
    PublicationEvidence,
)
from agent_comms.execution_states import (
    AttemptExecution,
    DeferredExecution,
    ExecutionState,
    FailedExecution,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.response_acknowledgement import canonical_publication_key
from agent_comms.messages import InfoKind, Message
from agent_comms.obligation_states import PublishedResponse, ResponseState
from agent_comms.wake_policy import WakePolicy
from test_coordination_store import ready, started


def members(family):
    """Only maintained production declarations; no parallel case inventory."""
    return tuple(c for c in family.members_with(family) if c.__module__.startswith("agent_comms."))


@pytest.fixture
def pending(tmp_path):
    with ready(tmp_path / "rules.sqlite3") as store:
        value = store.snapshots.get("exec")
        assert FieldCodec.decode(RecoverySnapshot, FieldCodec.encode(value)) == value
        yield value


@pytest.fixture
def active(tmp_path):
    with ready(tmp_path / "active.sqlite3") as store:
        started(store)
        value = store.snapshots.get("exec")
        assert value.is_current
        yield value


def rejected(value, diagnostic, **changes):
    with pytest.raises((ValueError, TypeError, IntegrityViolationError)):
        replace(value, **changes)


@pytest.mark.parametrize("field", ["wire_seq", "revision"])
def test_assignment_positive_numbers_and_timestamps(pending, field):
    row = pending.assignments[0]
    rejected(row, "must be positive", **{field: 0})
    rejected(row, "timestamps", accepted_at_ms=-1)
    rejected(row, "timestamps", updated_at_ms=row.accepted_at_ms - 1)
    assert replace(row, updated_at_ms=row.updated_at_ms + 1).revision > 0


@pytest.mark.parametrize("policy", members(WakePolicy), ids=lambda c: c.declared_name)
def test_assignment_decision_and_binding_owned_by_policy(policy):
    mode = policy()
    initial = policy.initial_state()
    state = initial.load(mode, None, None, None)
    assert state.mode == mode and state.exact_target is None
    with pytest.raises(IntegrityViolationError):
        initial.load(mode, "invalid-verdict", None, None)
    with pytest.raises(IntegrityViolationError):
        initial.load(mode, None, None, "unowned")
    if mode.active:
        for owner in members(AssignmentState):
            if issubclass(owner, BoundAssignment):
                bound = owner.build(mode, "exec", "owner")
                assert bound.execution_id == "exec" and bound.exact_target == "owner"
                for execution_id, target in ((None, "owner"), ("exec", None)):
                    with pytest.raises(IntegrityViolationError):
                        owner.build(mode, execution_id, target)


@pytest.mark.parametrize("owner", members(ExecutionState), ids=lambda c: c.declared_name)
def test_execution_attempt_reference_and_budget(owner, pending):
    if owner.unstarted:
        state = owner.load(None)
        with pytest.raises(IntegrityViolationError):
            owner.load(1)
    else:
        state = owner.load(1)
        if issubclass(owner, AttemptExecution):
            with pytest.raises(IntegrityViolationError):
                owner.load(None)
    assert state.current_attempt_ordinal in (None, 1)
    record = replace(pending.execution, lifecycle=state)
    assert record.max_attempts > 1
    if state.current_attempt_ordinal is not None:
        if owner.retry:
            rejected(record, "budget", max_attempts=1)
        with pytest.raises(
            (ValueError, IntegrityViolationError)
        ):
            owner.load(4).validate_budget(3)


def test_execution_numeric_time_and_route_boundaries(pending):
    row = pending.execution
    for field in ("revision", "max_attempts"):
        rejected(row, "must be positive", **{field: 0})
    rejected(row, "timestamps", created_at_ms=-1)
    rejected(row, "timestamps", updated_at_ms=row.created_at_ms - 1)
    for origin in ExecutionOrigin:
        assert replace(row, origin=origin).origin is origin
    route = pending.obligations[0]
    rejected(route, "exact_target", exact_target=None)
    rejected(route, "exact_target", exact_target="")


def test_replay_bits_revision_and_ambiguity(pending):
    row = ReplayAssessments("exec", ReplayFact.NONE, True, False, 1)
    rejected(row, "revision must be positive", revision=0)
    mask = sum(int(fact) for fact in ReplayFact)
    rejected(row, "unrecognized replay fact bits", facts=mask + 1)
    for fact in ReplayFact:
        with pytest.raises(IntegrityViolationError):
            replace(row, facts=fact)
        assert not replace(row, facts=fact, replay_safe=False).replay_safe


@pytest.mark.parametrize("owner", members(ResponseState), ids=lambda c: c.declared_name)
def test_response_receipt_and_publication_constraints(owner, pending):
    publication = published_snapshot(pending)
    intent = publication.publication_intents[0]
    acknowledgement = publication.publication_evidence[0]
    if owner.published:
        state = owner.load(acknowledgement.message_id, acknowledgement.seq)
        for message_id, seq in (
            (None, 1),
            ("acknowledgement", None),
            (None, None),
            ("", 1),
            ("acknowledgement", 0),
        ):
            with pytest.raises((ValueError, IntegrityViolationError)):
                owner.load(message_id, seq)
    else:
        state = owner.load(None, None)
        for message_id, seq in (("acknowledgement", None), (None, 1), ("acknowledgement", 1)):
            with pytest.raises(IntegrityViolationError):
                owner.load(message_id, seq)
    # Exhaust the declaration-owned publication contract without copying its roster.
    for has_intent, has_receipt in product((False, True), repeat=2):
        permitted = (
            (not has_intent or owner.allows_intent)
            and (has_intent or not owner.requires_intent)
            and has_receipt == owner.published
        )
        if permitted:
            state.validate_publication(
                intent if has_intent else None, acknowledgement if has_receipt else None
            )
        else:
            with pytest.raises(IntegrityViolationError):
                state.validate_publication(
                    intent if has_intent else None, acknowledgement if has_receipt else None
                )
    row = pending.obligations[0]
    rejected(row, "revision must be positive", revision=0)
    rejected(row, "timestamps", created_at_ms=-1)
    rejected(row, "timestamps", updated_at_ms=row.created_at_ms - 1)


def test_snapshot_membership_order_owner_and_disposition(pending):
    rejected(
        pending, "another execution",
        obligations=(replace(pending.obligations[0], execution_id="other"),)
    )
    claim = pending.assignments[0]
    rejected(
        pending,
        "another execution",
        assignments=(
            replace(
                claim, lifecycle=type(claim.lifecycle).build(claim.lifecycle.mode, "other", "owner")
            ),
        ),
    )
    link = pending.links[0]
    for changes in ({"ordinal": 1}, {"execution_id": "other"}, {"assignment_id": "other"}):
        rejected(pending, "ordered claims", links=(replace(link, **changes),))
    rejected(
        pending, "duplicate IDs", assignments=(claim, claim), links=(link, replace(link, ordinal=1))
    )
    rejected(pending, "wrong owner", assignments=(replace(claim, recipient_lookup="other"),))
    for owner in members(ExecutionState):
        if owner.assignment_state() is not type(claim.lifecycle):
            other = owner.assignment_state().build(claim.lifecycle.mode, "exec", "owner")
            rejected(pending, "disposition", assignments=(replace(claim, lifecycle=other),))


def test_snapshot_attempt_identity_pointer_and_version(active, pending):
    rejected(pending, "pointer_revision cannot be negative", pointer_revision=-1)
    rejected(pending, "unsupported snapshot version", snapshot_version=pending.snapshot_version + 1)
    rejected(active, "attempt does not match", attempt=None)
    rejected(pending, "attempt does not match", attempt=active.attempt)
    for changes in ({"execution_id": "other"}, {"attempt_ordinal": 2}, {"owner_lookup": "other"}):
        rejected(active, "identity/owner mismatch", attempt=replace(active.attempt, **changes))
    for owner in members(AttemptState):
        phase = owner.load(None if owner.terminal else 1000, owner.terminal, owner.terminal)
        altered = replace(active.attempt, lifecycle=phase)
        if owner.terminal:
            rejected(active, "status/attempt phase mismatch", attempt=altered)
        else:
            assert replace(active, attempt=altered).is_current
    for changes in ({"current_execution_id": NoPointedExecution()}, {"current_attempt_ordinal": NoPointedAttempt()}):
        rejected(active, "pointer tuple is incomplete", **changes)
    rejected(active, "pointer is inconsistent", is_current=False)
    rejected(
        active,
        "exact owner pointer",
        current_execution_id=NoPointedExecution(),
        current_attempt_ordinal=NoPointedAttempt(),
        is_current=False,
    )


def test_snapshot_wire_claimless_and_exact_routes(pending):
    rejected(pending, "wire snapshots require original obligations", obligations=())
    rejected(pending, "wire snapshots require original obligations", assignments=(), links=())
    rejected(
        pending,
        "claimless snapshots",
        execution=replace(pending.execution, origin=ExecutionOrigin.ACP),
    )
    rejected(
        pending,
        "response obligations differ from original claim routes",
        obligations=(replace(pending.obligations[0], exact_target="other"),),
    )
    claim = pending.assignments[0]
    rejected(
        pending,
        "response obligations differ from original claim routes",
        assignments=(
            replace(
                claim, lifecycle=type(claim.lifecycle).build(claim.lifecycle.mode, "exec", "other")
            ),
        ),
    )


def terminal_snapshot(active, execution_owner, attempt_owner, response_owner, *, replay_safe):
    phase = attempt_owner.load(None, True, True)
    execution = replace(active.execution, lifecycle=execution_owner.load(1))
    claims = tuple(
        replace(
            row,
            lifecycle=execution_owner.assignment_state().build(row.lifecycle.mode, "exec", "owner"),
        )
        for row in active.assignments
    )
    replay = ReplayAssessments("exec", ReplayFact.NONE, replay_safe, False, 1)
    return replace(
        active,
        execution=execution,
        attempt=replace(active.attempt, lifecycle=phase),
        assignments=claims,
        obligations=(replace(active.obligations[0], lifecycle=response_owner.load(None, None)),),
        replay=replay,
        current_execution_id=NoPointedExecution(),
        current_attempt_ordinal=NoPointedAttempt(),
        is_current=False,
    )


def test_snapshot_retry_terminal_coupling(active):
    failures = tuple(c for c in members(AttemptState) if c.failed)
    responses = tuple(
        c for c in members(ResponseState) if not c.published and not c.requires_intent
    )
    for phase, response in product(failures, responses):
        if response.retryable:
            value = terminal_snapshot(active, DeferredExecution, phase, response, replay_safe=True)
            # A failed terminal state is not automatically a retry predecessor.
            # Unknown outcomes remain unrunnable even with an otherwise safe ledger.
            assert value.can_retry is phase().permits_retry()
            rejected(value, "authorized retry", replay=replace(value.replay, replay_safe=False))
            with pytest.raises(
                IntegrityViolationError
            ):
                terminal_snapshot(active, FailedExecution, phase, response, replay_safe=True)
        else:
            with pytest.raises(IntegrityViolationError):
                terminal_snapshot(active, DeferredExecution, phase, response, replay_safe=True)
        failed = terminal_snapshot(active, FailedExecution, phase, response, replay_safe=False)
        assert not failed.can_retry
    for owner in members(ExecutionState):
        if owner.completed:
            for phase in (c for c in members(AttemptState) if c.succeeded):
                for response in responses:
                    if response.successful:
                        assert not terminal_snapshot(
                            active, owner, phase, response, replay_safe=True
                        ).can_retry
                    else:
                        with pytest.raises(
                            IntegrityViolationError
                        ):
                            terminal_snapshot(active, owner, phase, response, replay_safe=True)


def published_snapshot(pending):
    message = Message(
        sender="worker",
        target="owner",
        body="done",
        type=InfoKind,
        timestamp=1.0,
    )
    intent = PublicationIntents(
        "exec",
        "owner",
        message,
        canonical_publication_key("exec", "owner"),
        message.message_id,
    )
    acknowledgement = PublicationEvidence(
        execution_id="exec", exact_target="owner", seq=1,
        message_id=message.message_id, received_at_ms=0,
    )
    return replace(
        pending,
        obligations=(replace(pending.obligations[0], lifecycle=PublishedResponse(message.message_id, 1)),),
        publication_intents=(intent,),
        publication_evidence=(acknowledgement,),
    )


