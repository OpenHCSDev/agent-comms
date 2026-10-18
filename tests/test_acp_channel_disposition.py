"""Canonical channel notifications reflect each recipient's recorded outcome.

The existing model-boundary fixture supplies responses. ACP routing, source
identity, SQLite, native journals and notification projection are not mocked.
"""

import asyncio
from dataclasses import replace

import pytest

import agent_comms.private_nk_entrypoint as private_nk_entrypoint
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.activity import NativeInputDrainDiagnostic, UnavailableDrainDiagnostic
from agent_comms.native_input_record import FullNativeExecution, TriageNativeExecution
from agent_comms.selected_triage import FullSelectedTriage, IgnoreSelectedTriage
from agent_comms.coordinator import Coordination
from agent_comms.coordination_errors import StaleFence
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.notification_progress import SentProgress
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.goal_actions import SetGoalAction
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.tracked_turn import TrackedTurnSession
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.attempts import ReplayAssessments, ReplayFact
from delivery_owner_fixture import (
    canonical_delivery_owner,
    drain_diagnostic,
    native_model,
    reply_turn_finished,
)
from test_coordinated_runtime import tmp_path as private_root_fixture
from stored_inputs import stored_inputs

tmp_path = private_root_fixture


async def test_channel_outcomes_and_receipts_are_per_recipient_and_source(tmp_path, monkeypatch):
    monkeypatch.setattr(private_nk_entrypoint, "_trusted_package", lambda _: None)
    monkeypatch.setattr("agent_comms.native_pi._trusted_package", lambda _: None)
    async with canonical_delivery_owner(tmp_path) as (comms, owner, first, root_id):
        alpha, alpha_calls = native_model(decision="IGNORE")
        monkeypatch.setattr(TrackedTurnSession, "execute", alpha)
        assert await owner.inputs.drain_inbox("alpha") == 1
        await reply_turn_finished(owner, "alpha")
        beta, beta_calls = native_model(decision="FULL")
        monkeypatch.setattr(TrackedTurnSession, "execute", beta)
        assert await owner.inputs.drain_inbox("beta") == 1
        await reply_turn_finished(owner, "beta")
        assert len(alpha_calls) == 1 and len(beta_calls) == 2
        second = comms.messaging.send_message("sender", "#team", "A distinct source")
        monkeypatch.setattr(TrackedTurnSession, "execute", alpha)
        # Beta's prior reply is passive context, not another model request.
        assert await owner.inputs.drain_inbox("alpha") == 1
        await reply_turn_finished(owner, "alpha")
        with Coordination(str(comms.root / "coordination.sqlite3")) as store:
            rows = NativeRuntimeInput.select(store.session._connection)
            assert len(rows) == 4
            assert len({row.input_id for row in rows}) == 4
            assert all(row.reference.recorded for row in rows)

            def source_sequences(row):
                return tuple(
                    store.assignments.get(assignment_id).wire_seq
                    for assignment_id in row.execution.source_assignment_ids(
                        store.session._connection, row.input_id
                    )
                )

            alpha_lookup = RecordedThreadIncarnation("fixture", comms.registry.require("alpha").created_at).lookup
            beta_lookup = RecordedThreadIncarnation("fixture", comms.registry.require("beta").created_at).lookup
            by_source = {}
            for row in rows:
                by_source.setdefault((row.owner_lookup, source_sequences(row)), []).append(row)
            alpha_first = by_source[(alpha_lookup, (first.seq,))]
            beta_first = sorted(
                by_source[(beta_lookup, (first.seq,))],
                key=lambda row: row.execution.proof_order,
            )
            later_groups = [
                grouped
                for (owner_lookup, source_seqs), grouped in by_source.items()
                if owner_lookup == alpha_lookup and second.seq in source_seqs
            ]
            assert len(later_groups) == 1
            alpha_later = later_groups[0]
            assert [type(row.execution) for row in alpha_first] == [TriageNativeExecution]
            assert [type(row.execution) for row in beta_first] == [
                TriageNativeExecution,
                FullNativeExecution,
            ]
            assert [type(row.execution) for row in alpha_later] == [TriageNativeExecution]
            assert alpha_first[0].verdict is alpha_later[0].verdict is IgnoreSelectedTriage
            assert beta_first[0].verdict is FullSelectedTriage
            assert beta_first[1].execution.published_replies(
                store.session._connection, beta_lookup
            )
        assert stored_inputs(owner.inputs.dispositions.root).rows == {}
        assert not any(state.pending_turns for state in owner.inputs.drained.values())
        before = len(alpha_calls), len(beta_calls)
        assert await owner.inputs.drain_inbox("alpha") == 0
        assert (len(alpha_calls), len(beta_calls)) == before
        outcomes = comms.views.message_notifications_for_references((first.reference,))[(first.seq, first.message_id)]
        by_recipient = {row.recipient: row for row in outcomes}
        assert by_recipient["alpha"].state.startswith("Read ")
        assert by_recipient["beta"].state.startswith("Answered ")
        assert by_recipient["beta"].answered_at_ms is not None
        assert all(row.detail == "" and not row.busy for row in outcomes)


async def test_uncertain_channel_input_keeps_notification_and_never_replays(tmp_path, monkeypatch):
    monkeypatch.setattr(private_nk_entrypoint, "_trusted_package", lambda _: None)
    monkeypatch.setattr("agent_comms.native_pi._trusted_package", lambda _: None)
    model, calls = native_model(fail_on=1)
    monkeypatch.setattr(TrackedTurnSession, "execute", model)
    async with canonical_delivery_owner(tmp_path) as (comms, owner, message, root_id):
        assert await owner.inputs.drain_inbox("alpha") == 1
        with pytest.raises(NativePiUnavailable, match="fake backend process died"):
            await reply_turn_finished(owner, "alpha")
        failure = drain_diagnostic(comms, "alpha")
        assert isinstance(failure, NativeInputDrainDiagnostic)
        assert failure.error_type == "NativePiUnavailable" and "died" in failure.reason
        assert len(calls) == 1
        messages = comms.bus.inbox("alpha")
        comms.messaging.acknowledge("alpha", tuple(message.reference for message in messages))
        assert await owner.inputs.drain_inbox("alpha") == 0
        assert len(calls) == 1 and not any(state.pending_turns for state in owner.inputs.drained.values())
        with Coordination(str(comms.root / "coordination.sqlite3")) as store:
            rows = NativeRuntimeInput.read(
                store.session._connection.execute(f"SELECT * FROM {NativeRuntimeInput.declared_name}")
            )
            assert len(rows) == 1 and rows[0].session_id == "isolated-session"
            assert rows[0].session_entry_id is rows[0].llm_context_digest is None
            assert (
                read_historical_native_inputs(
                    store,
                    wire_root_id=root_id,
                    recipient_lookup=RecordedThreadIncarnation("fixture", comms.registry.require("alpha").created_at).lookup,
                    source_seq=message.seq,
                )
                == ()
            )
        notices = comms.views.message_notifications_for_references((message.reference,))[(message.seq, message.message_id)]
        alpha = next(row for row in notices if row.recipient == "alpha")
        assert isinstance(alpha.state, SentProgress)
        assert not alpha.state.working


@pytest.mark.parametrize("revocation", ["goal", "project", "stop"])
async def test_selected_owner_revocation_before_send_never_creates_receipt(
    tmp_path, monkeypatch, revocation
):
    monkeypatch.setattr(private_nk_entrypoint, "_trusted_package", lambda _: None)
    monkeypatch.setattr("agent_comms.native_pi._trusted_package", lambda _: None)
    model, calls = native_model()
    entered, release = asyncio.Event(), asyncio.Event()

    async def suspended(*args, **kwargs):
        entered.set()
        await release.wait()
        return await model(*args, **kwargs)

    monkeypatch.setattr(TrackedTurnSession, "execute", suspended)
    async with canonical_delivery_owner(tmp_path, direct=True) as (comms, owner, message, _root):
        assert await owner.inputs.drain_inbox("beta") == 1
        turn = owner.inputs.state("beta").wake_task
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if revocation == "goal":
                comms.goals.update_goal("beta", SetGoalAction(text="New independent task"))
            elif revocation == "project":
                project = tmp_path / "other-project"
                project.mkdir()
                comms.threads.set_project("beta", str(project))
            else:
                comms.registry.unregister("beta")
            release.set()
            with pytest.raises(StaleFence, match="recipient registry owner changed before native send"):
                await asyncio.wait_for(turn, 5)
            failure = drain_diagnostic(comms, "beta")
            if revocation == "stop":
                assert failure is None  # The stopped owner has no remaining activity view.
            else:
                assert isinstance(failure, UnavailableDrainDiagnostic)
                assert failure.error_type == "StaleFence"
            assert calls == []
            assert await owner.inputs.drain_inbox("beta") == 0
            assert calls == []
            with Coordination(str(comms.root / "coordination.sqlite3")) as store:
                rows = NativeRuntimeInput.read(
                    store.session._connection.execute(f"SELECT * FROM {NativeRuntimeInput.declared_name}")
                )
                assert len(rows) == 1 and rows[0].session_id is None
            assert comms.bus.log.messages_for_references((message.reference,)) == (message,)
            assert not any(state.pending_turns for state in owner.inputs.drained.values())
        finally:
            release.set()
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)


async def test_notification_busy_requires_matching_live_process_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(private_nk_entrypoint, "_trusted_package", lambda _: None)
    monkeypatch.setattr("agent_comms.native_pi._trusted_package", lambda _: None)
    model, calls = native_model()
    entered, release = asyncio.Event(), asyncio.Event()

    async def suspended(*args, **kwargs):
        entered.set()
        await release.wait()
        return await model(*args, **kwargs)

    monkeypatch.setattr(TrackedTurnSession, "execute", suspended)
    async with canonical_delivery_owner(tmp_path, direct=True) as (comms, owner, message, root_id):
        assert await owner.inputs.drain_inbox("beta") == 1
        turn = owner.inputs.state("beta").wake_task
        try:
            await asyncio.wait_for(entered.wait(), 5)
            key = message.seq, message.message_id
            notices = comms.views.message_notifications_for_references((message.reference,))[key]
            assert len(notices) == 1
            assert notices[0].state == "Sent"
            assert notices[0].detail == "" and notices[0].busy
            assert comms.views.thread_presentations(("beta",)).require("beta").notifications == (
                replace(notices[0], message=message),
            )
            assert calls == []  # Projection never launches or resends an input.

            with Coordination(str(comms.root / "coordination.sqlite3")) as store:
                assignment = next(
                    row for row in WakeAssignment.select(
                        store.session._connection, where="wire_seq=?", parameters=(message.seq,)
                    ) if row.recipient == "beta"
                )
                with store.session.transaction():
                    ReplayAssessments.accumulate(
                        store.session._connection,
                        assignment.lifecycle.execution_id,
                        ReplayFact.ASSISTANT_OUTPUT,
                    )
            # Output is not a competing read authority. Until the native
            # context acknowledgement exists, even output evidence remains not-read.
            working = comms.views.message_notifications_for_references((message.reference,))[key]
            assert working[0].state == "Sent"
            assert working[0].detail == "" and working[0].busy

            held = comms.messaging.send_message(
                "sender", "beta", "wait behind the recipient's unresolved turn"
            )
            with Coordination(str(comms.root / "coordination.sqlite3")) as store:
                accepted = accept_delivery_cohort(comms.bus, root_id, held.seq, store)
                assert accepted.value.member_count == 1
            queued, = comms.views.message_notifications_for_references((held.reference,))[
                held.seq, held.message_id
            ]
            assert queued.state.startswith("Queued ")
            assert queued.detail == "" and not queued.busy

            # Same PID, different process incarnation: even a retained active turn
            # cannot supply liveness. Persist through the real typed registry.
            with comms.registry.store.editing() as edit:
                current = edit.document.threads["beta"]
                identity = current.process_identity
                assert identity is not None and current.active_turn.executing
                edit.document.threads["beta"] = replace(
                    current, process_identity=replace(identity, start_time=identity.start_time + 1)
                )
                edit.commit()
            paused = comms.views.message_notifications_for_references((message.reference,))[key]
            assert paused[0].state == "Sent"
            assert paused[0].detail == "" and not paused[0].busy
            assert replace(paused[0], message=message) in comms.views.thread_presentations(("beta",)).require("beta").notifications
            assert calls == []
        finally:
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
