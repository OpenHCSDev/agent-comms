"""Canonical channel notifications reflect each recipient's recorded outcome.

The existing model-boundary fixture supplies responses. ACP routing, source
identity, SQLite, native journals and notification projection are not mocked.
"""

import asyncio
from dataclasses import replace

import pytest

from agent_comms import cohort_foreground
from agent_comms.activity import NativeInputDrainDiagnostic, UnavailableDrainDiagnostic
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.native_input_record import FullNativeExecution, TriageNativeExecution
from agent_comms.selected_triage import FullSelectedTriage, IgnoreSelectedTriage
from agent_comms.coordinator import Coordination
from agent_comms.goal_actions import SetGoalAction
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.tracked_turn import TrackedTurnSession
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.attempts import ReplayAssessments, ReplayFact
from agent_comms.turn_phase import ResponseOutputPhase
from delivery_owner_fixture import (
    canonical_delivery_owner,
    drain_diagnostic,
    native_model,
    reply_turn_finished,
)
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


async def test_channel_outcomes_and_receipts_are_per_recipient_and_source(tmp_path, monkeypatch):
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
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

            alpha_lookup = stable_thread_lookup(comms.registry.require("alpha").created_at)
            beta_lookup = stable_thread_lookup(comms.registry.require("beta").created_at)
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
        assert owner.inputs.dispositions.read().rows == {}
        assert owner.inputs.pending_turns == {}
        before = len(alpha_calls), len(beta_calls)
        assert await owner.inputs.drain_inbox("alpha") == 0
        assert (len(alpha_calls), len(beta_calls)) == before
        outcomes = comms.views.message_notifications([first])[(first.seq, first.message_id)]
        assert {row.recipient: row.state for row in outcomes} == {
            "alpha": "Checked — no response",
            "beta": "Responded",
        }
        assert not any(row.busy for row in outcomes)


async def test_uncertain_channel_input_keeps_notification_and_never_replays(tmp_path, monkeypatch):
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr("agent_comms.native_pi._trusted_package", lambda _: None)
    model, calls = native_model(fail_on=1)
    monkeypatch.setattr(TrackedTurnSession, "execute", model)
    async with canonical_delivery_owner(tmp_path) as (comms, owner, message, root_id):
        assert await owner.inputs.drain_inbox("alpha") == 1
        await reply_turn_finished(owner, "alpha")
        failure = drain_diagnostic(comms, "alpha")
        assert isinstance(failure, NativeInputDrainDiagnostic)
        assert failure.error_type == "NativePiUnavailable" and "died" in failure.reason
        assert len(calls) == 1
        comms.messaging.acknowledge("alpha")
        assert await owner.inputs.drain_inbox("alpha") == 0
        assert len(calls) == 1 and not owner.inputs.pending_turns
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
                    recipient_lookup=stable_thread_lookup(
                        comms.registry.require("alpha").created_at
                    ),
                    source_seq=message.seq,
                )
                == ()
            )
        notices = comms.views.message_notifications([message])[(message.seq, message.message_id)]
        alpha = next(row for row in notices if row.recipient == "alpha")
        assert alpha.state not in {"Responded", "Checked — no response"}
        assert not alpha.busy


@pytest.mark.parametrize("revocation", ["goal", "project", "stop"])
async def test_selected_owner_revocation_before_send_never_creates_receipt(
    tmp_path, monkeypatch, revocation
):
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
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
        turn = owner.inputs.wake_tasks["beta"]
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
            assert comms.bus.log.message_by_id(message.message_id) == message
            assert not owner.inputs.pending_turns
        finally:
            release.set()
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)


async def test_notification_busy_requires_matching_live_process_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
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
        turn = owner.inputs.wake_tasks["beta"]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            key = message.seq, message.message_id
            notices = comms.views.message_notifications([message])[key]
            assert len(notices) == 1
            assert notices[0].state == "Starting response" and notices[0].busy
            assert comms.views.recent_notifications("beta") == (
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
            working = comms.views.message_notifications([message])[key]
            assert working[0].state == "Read - working" and working[0].busy

            # Same PID, different process incarnation: even a retained active turn
            # cannot supply liveness. Persist through the real typed registry.
            with comms.registry.store.editing() as edit:
                current = edit.document.threads["beta"]
                identity = current.process_identity
                assert identity is not None and current.active_turn is not None
                edit.document.threads["beta"] = replace(
                    current, process_identity=replace(identity, start_time=identity.start_time + 1)
                )
                edit.commit()
            paused = comms.views.message_notifications([message])[key]
            assert paused[0].state == "Read — interrupted, outcome unknown" and not paused[0].busy
            assert comms.views.recent_notifications("beta") == (
                replace(paused[0], message=message),
            )
            assert calls == []
        finally:
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
