"""Canonical channel notifications reflect each recipient's recorded outcome.

The existing model-boundary fixture supplies responses. ACP routing, source
identity, SQLite, native journals and notification projection are not mocked.
"""

import asyncio
from dataclasses import replace

import pytest

from agent_comms import cohort_foreground, coordinated_runtime
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.coordination_store import MutationStore, StaleFence
from agent_comms.goal_actions import SetGoalAction
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_runtime_input import NativeRuntimeInput
from delivery_owner_fixture import canonical_delivery_owner, native_model
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


async def test_channel_outcomes_and_receipts_are_per_recipient_and_source(tmp_path, monkeypatch):
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    async with canonical_delivery_owner(tmp_path) as (comms, owner, first, root_id):
        alpha, alpha_calls = native_model(decision="IGNORE")
        monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", alpha)
        assert await owner.inputs.drain_inbox("alpha") == 1
        beta, beta_calls = native_model(decision="FULL")
        monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", beta)
        assert await owner.inputs.drain_inbox("beta") == 1
        assert len(alpha_calls) == 1 and len(beta_calls) == 2
        second = comms.messaging.send_message("sender", "#team", "A distinct source")
        monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", alpha)
        # Beta's prior reply is passive context, not another model request.
        assert await owner.inputs.drain_inbox("alpha") == 1
        with MutationStore(str(comms.root / "coordination.sqlite3")) as store:

            def evidence(name, message):
                return read_historical_native_inputs(
                    store,
                    wire_root_id=root_id,
                    recipient_lookup=stable_thread_lookup(comms.registry.require(name).created_at),
                    source_seq=message.seq,
                )

            a, b, later = (
                evidence("alpha", first),
                evidence("beta", first),
                evidence("alpha", second),
            )
            assert [row.stage for row in a] == ["triage"]
            assert [row.stage for row in b] == ["triage", "full"]
            assert [row.stage for row in later] == ["triage"]
            assert a[0].triage_result == later[0].triage_result == "ignore"
            assert len({row.input_id for row in (*a, *b, *later)}) == 4
            assert all(row.expected_prompt_equality_established for row in (*a, *b, *later))
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
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    model, calls = native_model(fail_on=1)
    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", model)
    async with canonical_delivery_owner(tmp_path) as (comms, owner, message, root_id):
        with pytest.raises(NativePiUnavailable, match="died"):
            await owner.inputs.drain_inbox("alpha")
        assert len(calls) == 1
        comms.messaging.acknowledge("alpha")
        assert await owner.inputs.drain_inbox("alpha") == 0
        assert len(calls) == 1 and not owner.inputs.pending_turns
        with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
            rows = NativeRuntimeInput.read(
                store._connection.execute(f"SELECT * FROM {NativeRuntimeInput.declared_name}")
            )
            assert len(rows) == 1 and rows[0].session_id is None
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
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    model, calls = native_model()
    entered, release = asyncio.Event(), asyncio.Event()

    async def suspended(*args, **kwargs):
        entered.set()
        await release.wait()
        return await model(*args, **kwargs)

    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", suspended)
    async with canonical_delivery_owner(tmp_path, direct=True) as (comms, owner, message, _root):
        turn = asyncio.create_task(owner.inputs.drain_inbox("beta"))
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
            with pytest.raises(StaleFence):
                await asyncio.wait_for(turn, 5)
            assert calls == []
            assert await owner.inputs.drain_inbox("beta") == 0
            assert calls == []
            with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
                rows = NativeRuntimeInput.read(
                    store._connection.execute(f"SELECT * FROM {NativeRuntimeInput.declared_name}")
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
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    model, calls = native_model()
    entered, release = asyncio.Event(), asyncio.Event()

    async def suspended(*args, **kwargs):
        entered.set()
        await release.wait()
        return await model(*args, **kwargs)

    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", suspended)
    async with canonical_delivery_owner(tmp_path, direct=True) as (comms, owner, message, _root):
        turn = asyncio.create_task(owner.inputs.drain_inbox("beta"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            key = message.seq, message.message_id
            notices = comms.views.message_notifications([message])[key]
            assert len(notices) == 1
            assert notices[0].state == "Responding…" and notices[0].busy
            assert comms.views.recent_notifications("beta") == (
                replace(notices[0], message=message),
            )
            assert calls == []  # Projection never launches or resends an input.

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
            assert paused[0].state == "Paused" and not paused[0].busy
            assert comms.views.recent_notifications("beta") == (
                replace(paused[0], message=message),
            )
            assert calls == []
        finally:
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
