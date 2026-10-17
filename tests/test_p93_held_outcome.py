from contextlib import contextmanager
from types import SimpleNamespace
import asyncio

import pytest

from agent_comms.agent_backend import InputContent, InputId, InputRequest, TurnInbox, WhenBusy
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator, DrainingPhase
from agent_comms.send_outcome import AuthorityChangedSend, RefusedSend
from agent_comms.turn_inputs import InputForwarding


@pytest.mark.asyncio
async def test_held_input_retains_queue_and_registers_pending_only_after_write(tmp_path):
    inbox = TurnInbox()
    inputs = InputForwarding(queue=inbox)
    writes = []
    class Writer:
        def write(self, value):
            assert not inputs.pending
            writes.append(value)
        async def drain(self):
            pass
    session = SimpleNamespace(startup=SimpleNamespace(root=tmp_path), send_boundary=None,
        stdin=Writer(), native=SimpleNamespace(reader=SimpleNamespace(encode=lambda value: value)),
        output=SimpleNamespace(sensitive=False))
    gate = MaintenanceBarrier(tmp_path/'registry.json')
    hold = gate.begin(MaintenanceOperator('p93-test'))
    request = InputRequest(input_id=InputId('fresh'), content=InputContent(text='new input'), when_busy=WhenBusy.FOLLOW_UP)
    assert await inputs.place(session, request)
    assert inbox.qsize() == 1 and not inputs.pending and writes == []
    assert not inputs.all_written_started
    async with gate.send(None, None, 'id', 'text') as outcome:
        assert isinstance(outcome, DrainingPhase)
    gate.clear(hold)
    async with gate.admit_ingress_async() as status:
        inbox.maintenance_changed(status)
    assert await inputs.place(session, await inbox.get())
    assert len(writes) == 1 and len(inputs.pending) == 1 and inputs.generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', [AuthorityChangedSend(), RefusedSend()])
async def test_authority_loss_and_never_sent_refusal_remain_distinct(tmp_path, decision):
    @contextmanager
    def boundary(*args):
        yield decision
    failures = []
    session = SimpleNamespace(startup=SimpleNamespace(root=tmp_path), send_boundary=boundary,
        output=SimpleNamespace(final_assistant_stop=True, record_failure=failures.append),
        rejected_commands=[], rejected_signal=asyncio.Event())
    inputs = InputForwarding(queue=TurnInbox())
    request = InputRequest(input_id=InputId('fresh'), content=InputContent(text='new input'), when_busy=WhenBusy.FOLLOW_UP)
    result = await inputs.place(session, request)
    assert not inputs.pending and inputs.generation == 0
    if isinstance(decision, AuthorityChangedSend):
        assert result is False and inputs.uncertain and len(failures) == 1
        assert session.rejected_commands == []
    else:
        assert result is True and not inputs.uncertain and failures == []
        assert [event.id for event in session.rejected_commands] == ['fresh']
