"""Original Codex input observations and failed turns through their actual owners."""
from dataclasses import replace

import pytest

from agent_comms.cli_bridges.codex import CodexBridge, CodexOptions
from agent_comms.cli_bridges.codex_protocol import BusClientId, ItemStream, RpcNotification
from agent_comms.cli_bridges.harness_delivery_states import AcceptedOutcome, DeliveredOutcome, ObservedBridgeInput, UnconfirmedOutcome
from agent_comms.cli_bridges.runtime_updates import FailedTurn, FinishedTurn, ThreadOutput
from agent_comms.cli_bridges.shared import Binding, IncomingFrame, StoredBinding, Watch
from agent_comms.execution.turn_item import NativeItemId, NativeTurnId
from agent_comms.messaging.notification_evidence import AllOutcomes
from agent_comms.messaging.notification_progress import HarnessConsumedProgress
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import ExternalThread
from agent_comms.transcript.transcript_events import NoticeTranscript
from conftest import installed_comms
from test_coordinated_runtime import _root


@pytest.mark.parametrize('echo_first', [False, True])
async def test_original_input_proof_survives_either_acceptance_order(tmp_path, echo_first):
    root, _, comms, initial, people = _root(tmp_path, direct=True)
    message = initial.message
    binding = Binding(root=str(root), owner=people[2].incarnation, thread='codex-thread', start=Binding.bus_head(str(root)))
    with Binding.open(tmp_path / 'bridge') as db:
        options = CodexOptions(Watch(), StoredBinding(), tmp_path / 'bridge', tmp_path, str(tmp_path / 'unused.sock'))
        bridge = CodexBridge(options, binding.address(), db)
        await options.operation.prepare(bridge)
        original = bridge.attempt(message, UnconfirmedOutcome)
        assert await bridge.coordinator_write(original.begin)
        stream = ItemStream('codex-thread')
        observed = stream.receive(RpcNotification(method='item/completed', params={
            'threadId': 'codex-thread', 'turnId': 'actual-turn', 'completedAtMs': 2000,
            'item': {'type': 'userMessage', 'id': 'actual-input',
                     'clientId': BusClientId(message.message_id).external,
                     'content': [{'type': 'text', 'text': IncomingFrame(message).text}]},
        }))
        if echo_first:
            await observed.observed(bridge)
        await bridge.finish(original, lambda row: replace(row, outcome=AcceptedOutcome))
        if not echo_first:
            await observed.observed(bridge)
        retained, = await bridge.attempts(message.message_id)
        assert retained.outcome is DeliveredOutcome
        assert retained.monitor == ObservedBridgeInput(NativeTurnId('actual-turn'), NativeItemId('actual-input'), 2.0)
        assert not await bridge.coordinator_write(original.begin)
        current, = comms.views.message_notifications_for_references(AllOutcomes(), (message.reference,)).outcomes[message.seq, message.message_id]
        assert current.state.is_consumed()
        assert current.label == current.timed_state(HarnessConsumedProgress.label, 2000)


def test_failed_turn_retains_the_original_error_without_an_answer(tmp_path):
    comms = installed_comms(tmp_path / 'root')
    comms.registry.declare(ExternalThread('cli', frozenset(), Checkout(tmp_path)))
    output = ThreadOutput(comms.transcripts.external('cli'), comms.agents)
    stream = ItemStream('codex-thread')
    message = 'Error running remote compact task: This request was blocked by our safety systems. Reason: Potentially unintended activity.'
    notification = RpcNotification(method='turn/completed', params={
        'threadId': 'codex-thread', 'turn': {'id': 'actual-turn', 'status': 'failed',
        'error': {'message': message, 'codexErrorInfo': 'misalignmentPolicyViolation'}},
    })
    assert ItemStream('another-thread').receive(notification) is None
    failure = stream.receive(notification)
    assert isinstance(failure, FailedTurn)
    output.publish_batch([failure])
    page = comms.transcripts.capture_page_read('cli').read()
    notice, = page.events
    assert isinstance(notice, NoticeTranscript)
    assert notice.text == message
    assert not notice.answers_turn
    output.publish_batch([failure])
    assert len(comms.transcripts.capture_page_read('cli').read().events) == 1
    assert stream.receive(RpcNotification(method='turn/completed', params={
        'threadId': 'codex-thread', 'turn': {'id': 'normal', 'status': 'completed', 'error': None},
    })) == FinishedTurn(NativeTurnId('normal'))
