"""A dead owner's unattempted frozen reply is retained, never sent or replayed."""
from __future__ import annotations

import asyncio
import multiprocessing
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_comms.comms import wire
from agent_comms.coordination.attempt_recovery import RecoveryMonitorCapability, VerifiedOwnerLoss
from agent_comms.coordination.attempt_states import ModelRunningAttempt, PromptAcceptedAttempt, UnknownOutcomeAttempt
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.errors import CoordinationError, RecoveryBlocked, ResponseNotDispatched
from agent_comms.coordination.obligation_states import FailedResponse
from agent_comms.coordination.response import LiveResponseOwner, PublicationAppendDispatches, prepare_fenced_response, publish_fenced_response
from agent_comms.coordination.tables.attempts import ReplayFact
from agent_comms.coordination.tables.executions import CurrentExecutions
from agent_comms.execution.native_runtime_input import NativeRuntimeInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.storage.private_path import FileRevision
from test_coordination_response import _ready
from test_native_failure_recovery import ClaimedHistory, release_native_input
from test_native_unknown_recovery import input_evidence, leave


class FrozenReplyHistory(ClaimedHistory):
    mode = "frozen"

    @classmethod
    def record(cls, claim, session, participant):
        with Coordination(claim.store_path) as store:
            progress = claim.stage.progress
            with progress.using_attempts(store):
                progress.advance(PromptAcceptedAttempt)
                progress.advance(ModelRunningAttempt)
                fence = progress.finish()
            witness = LiveResponseOwner(thread=participant.owner.thread,
                                        claimed_generation=participant.owner.claimed_generation)
            target = store.get(fence.execution_id).require_wire_responses()[0].exact_target
            intent = prepare_fenced_response(store, participant.bus, fence, "Original frozen answer",
                                            exact_target=target, owner_witness=witness).value
            if cls.mode == "frozen":
                return
            if cls.mode == "mismatched":
                with store.session.writing() as db:
                    PublicationAppendDispatches(fence.execution_id, target, participant.root_id,
                                               fence.owner_generation + 1, fence.attempt_ordinal, 1000).insert(db)
                return
            target_method = ("_publish_keyed_response_unlocked" if cls.mode == "attempted" else "complete")
            owner = participant.bus.publisher if cls.mode == "attempted" else PublicationAppendDispatches
            with patch.object(owner, target_method, side_effect=OSError("original publication interrupted")):
                with pytest.raises(OSError, match="original publication interrupted"):
                    publish_fenced_response(store, participant.bus, fence,
                                            exact_target=target, owner_witness=witness)
            assert (intent.published_message(participant.bus.bus) is not None) == (cls.mode == "keyed")


def frozen_owner(directory, mode, output, exit_allowed):
    FrozenReplyHistory.mode = mode
    source = asyncio.run(release_native_input(Path(directory), FrozenReplyHistory))
    with closing(output):
        output.send((str(source.root), source.execution_id))
    if not exit_allowed.wait(20):
        raise TimeoutError("fixture owner must be released")


@pytest.fixture(params=("frozen", "attempted", "mismatched", "keyed"))
def released_frozen(tmp_path, request):
    context = multiprocessing.get_context("spawn")
    output, announcement = context.Pipe(duplex=False)
    exit_allowed = context.Event()
    process = context.Process(target=frozen_owner, args=(str(tmp_path), request.param, announcement, exit_allowed))
    process.start()
    announcement.close()
    try:
        with closing(output):
            root, execution_id = output.recv()
        leave(process, exit_allowed)
        yield Path(root), execution_id, request.param
    finally:
        leave(process, exit_allowed)


def test_original_owner_recovery_distinguishes_unattempted_and_uncertain_publication(released_frozen):
    root, execution_id, mode = released_frozen
    comms = wire(root)
    owner = comms.registry.require("beta")
    bus_revision = comms.bus.bus.revision()
    dispositions = FileRevision.capture(InputDispositions.path_in(root))
    with Coordination(root / "coordination.sqlite3") as store:
        before = store.get(execution_id)
        inputs = input_evidence(store)
        assert NativeRuntimeInput.count(store.session._connection) == 1
    if mode in ("attempted", "mismatched"):
        with pytest.raises(CoordinationError):
            RecoveryMonitorCapability.recover_owners(comms.owners, (owner,))
        with Coordination(root / "coordination.sqlite3") as store:
            assert store.get(execution_id) == before
            assert input_evidence(store) == inputs
    else:
        RecoveryMonitorCapability.recover_owners(comms.owners, (owner,))
        with Coordination(root / "coordination.sqlite3") as store:
            after = store.get(execution_id)
            assert not after.is_current
            assert after.publication_intents == before.publication_intents
            assert input_evidence(store) == inputs
            if mode == "frozen":
                assert isinstance(after.attempt.lifecycle, UnknownOutcomeAttempt)
                assert all(isinstance(row.lifecycle, FailedResponse) for row in after.obligations)
                assert after.replay.require_assessment().facts & ReplayFact.UNKNOWN_EFFECTS
                assert not after.replay.require_assessment().replay_safe
                assert not after.publication_evidence
                assert PublicationAppendDispatches.count(store.session._connection) == 0
                # Re-enter the ordinary preparation resolver after recovery: a retained
                # failed reply must not become a permanent restart trap.
                with store.session.capture(comms.bus) as boundary:
                    assert PublicationAppendDispatches.resolve_retained(
                        store, comms.bus, boundary, owner_lookup=owner.incarnation.lookup) == ()
            else:
                assert after.completed_response()
                assert len(after.publication_evidence) == 1
            pointer = CurrentExecutions.require(store.session._connection, owner_lookup=owner.incarnation.lookup)
            assert not pointer.unresolved
        RecoveryMonitorCapability.recover_owners(comms.owners, (owner,))
    assert comms.bus.bus.revision() == bus_revision
    assert FileRevision.capture(InputDispositions.path_in(root)) == dispositions


def test_live_owner_cannot_retire_frozen_reply_and_partial_publication_is_not_unattempted(tmp_path):
    case = _ready(tmp_path, extra_targets=("owner",))
    try:
        routes = tuple(row.exact_target for row in case.store.get("exec").require_wire_responses())
        for target in routes:
            prepare_fenced_response(case.store, case.bus, case.fence, "Frozen route answer",
                                    exact_target=target, owner_witness=case.witness)
        with pytest.raises(ResponseNotDispatched), case.store.session.capture(case.bus) as boundary:
            PublicationAppendDispatches.resolve_retained(case.store, case.bus, boundary,
                                                        owner_lookup=case.owner_lookup)
        with pytest.raises(RecoveryBlocked):
            with VerifiedOwnerLoss.observe_native_release(case.store, "exec"):
                pytest.fail("a live owner is not a released owner")
        publish_fenced_response(case.store, case.bus, case.fence,
                                exact_target=routes[0], owner_witness=case.witness)
        with case.store.session.writing():
            assert not PublicationAppendDispatches.unattempted(case.store, case.bus, case.store.get("exec"))
        assert case.store.get("exec").is_current
    finally:
        case.close()
