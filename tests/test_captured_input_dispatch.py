"""Captured admissions survive only their exact live owner; dispatch failures stay uncertain."""

import asyncio
from contextlib import asynccontextmanager

from agent_comms.queued_input import InitialInput, InputReservation
import pytest

from agent_comms.acp_extension import InputDeliveryChangedUpdate, decode_updates
from agent_comms.errors import RelationViolationError
from agent_comms.queued_input import InputHandoffRefused
from agent_comms.input_attempt import NotSentInput, ReservedInput
from test_acp_queue_contract import _owner
from stored_inputs import stored_inputs


async def test_unplaced_reservation_retires_its_captured_original(tmp_path):
    comms, agent, _, _ = _owner(tmp_path)
    try:
        async with InitialInput.reserve(
            agent.inputs, "beta", "beta", text="Unplaced", prompt="Unplaced",
            echo=True, images=(), controller=None,
        ) as reservation:
            assert isinstance(reservation, InputReservation)
            assert isinstance(reservation.item, InitialInput)
            assert reservation.owner == comms.registry.require("beta")
            assert reservation.source.key == reservation.item.key
            assert isinstance(reservation.source, ReservedInput)
            assert stored_inputs(agent.inputs.dispositions.root).lookup(
                reservation.item.key
            ) == reservation.source
        retired = stored_inputs(agent.inputs.dispositions.root).lookup(reservation.item.key)
        assert isinstance(retired, NotSentInput) and not retired.has_native_binding
    finally:
        await agent.shutdown()


async def test_released_reservation_transfers_rollback_to_its_adopting_scope(tmp_path):
    _, agent, _, _ = _owner(tmp_path)
    try:
        async with InitialInput.reserve(
            agent.inputs, "beta", "beta", text="Transferred", prompt="Transferred",
            echo=True, images=(), controller=None,
        ) as reservation:
            rollback = reservation.release()
        assert stored_inputs(agent.inputs.dispositions.root).lookup(
            reservation.item.key
        ) == reservation.source
        await rollback.aclose()
        retired = stored_inputs(agent.inputs.dispositions.root).lookup(reservation.item.key)
        assert isinstance(retired, NotSentInput) and not retired.has_native_binding
    finally:
        await agent.shutdown()


async def test_initial_capture_rejects_owner_readmission_during_acceptance_notice(
    tmp_path, monkeypatch
):
    comms, agent, _, _ = _owner(tmp_path)
    admitted = []
    dispatched = []

    class Client:
        async def session_update(self, **kwargs):
            notices = decode_updates(kwargs["update"].field_meta)
            for notice in notices:
                if isinstance(notice, InputDeliveryChangedUpdate):
                    admitted.append(notice.input_id)
                    owner = comms.registry.require("beta")
                    comms.registry.register(owner, new_owner=True)

    async def forbidden(*args, **kwargs):
        dispatched.append(args)
        raise AssertionError("Changed captured owner must not reach the native dispatcher")

    agent.on_connect(Client())
    monkeypatch.setattr(agent.turns, "run_agent_turn", forbidden)
    try:
        with pytest.raises(InputHandoffRefused, match="context changed"):
            await InitialInput.run(agent.inputs, "beta", "beta", "Fresh captured input")
        assert len(admitted) == 1 and not dispatched
        row = stored_inputs(agent.inputs.dispositions.root).lookup("acp:" + admitted[0])
        assert isinstance(row, NotSentInput) and not row.has_native_binding
        assert row.source_text == "Fresh captured input"
        assert not agent.inputs.queued_inputs["beta"]
        assert not agent.turns.turn_tasks
    finally:
        await agent.shutdown()


async def test_postdispatch_failure_is_not_reclassified_as_admission_refusal(tmp_path, monkeypatch):
    comms, agent, _, _ = _owner(tmp_path)
    entered = []
    failure = RelationViolationError("late uncertainty after dispatcher entry")

    async def uncertain(*args, **kwargs):
        entered.append(kwargs["original_keys"])
        raise failure

    monkeypatch.setattr(agent.turns, "run_agent_turn", uncertain)
    try:
        with pytest.raises(RelationViolationError) as caught:
            await InitialInput.run(agent.inputs, "beta", "beta", "One explicit request")
        assert caught.value is failure and not isinstance(caught.value, InputHandoffRefused)
        assert len(entered) == 1
        assert len(stored_inputs(agent.inputs.dispositions.root).rows) == 1
    finally:
        await agent.shutdown()


async def test_initial_transfer_cancellation_retires_original_before_dispatch(tmp_path, monkeypatch):
    comms, agent, _, _ = _owner(tmp_path)
    reserve = InitialInput.reserve

    @asynccontextmanager
    async def cancelled_exit(*args, **kwargs):
        async with reserve(*args, **kwargs) as captured:
            yield captured
            # The caller transferred reservation custody, but original async
            # scope exit has not completed. No dispatcher/native input ran.
            raise asyncio.CancelledError

    monkeypatch.setattr(InitialInput, "reserve", cancelled_exit)
    try:
        with pytest.raises(asyncio.CancelledError):
            await InitialInput.run(agent.inputs, "beta", "beta", "Never dispatched")
        document = stored_inputs(agent.inputs.dispositions.root)
        assert len(document.rows) == 1
        row = next(iter(document.rows.values()))
        assert isinstance(row, NotSentInput) and row.source_text == "Never dispatched"
        assert not row.has_native_binding
        assert not agent.inputs.queued_inputs["beta"]
        assert not agent.turns.turn_tasks
    finally:
        await agent.shutdown()
