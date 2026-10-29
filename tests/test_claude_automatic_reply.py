"""A session's final answers a DM, never an outstanding channel mention."""

import pytest

from agent_comms.cli_bridges.claude_protocol import ClaudeSession, KnownTurn
from agent_comms.harness_delivery_states import DeliveredOutcome
from agent_comms.turn_item import NativeTurnId
from transcript_record_fixture import transcript_record
from test_monitor_delivery import attached_monitor, bridge, decoded_record, notification


@pytest.mark.asyncio
async def test_channel_mention_and_dm_share_a_turn_without_broadcasting_the_final(bridge):
    comms = bridge.comms
    comms.channels.create_tag('comms')
    for name in ('claude-code', 'viewer', 'peer'):
        comms.channels.update_tags(name, add=frozenset({'comms'}))
    channel = comms.messaging.send('peer', '#comms', '@viewer please investigate').message
    direct = comms.messaging.send('claude-code', 'viewer', 'please answer me privately').message
    output = bridge.output_type.for_comms(comms.transcripts.external('viewer'), comms)
    session = ClaudeSession(turn=KnownTurn(NativeTurnId('reply-turn')))
    async with attached_monitor(bridge) as reader:
        for message in (channel, direct):
            await bridge.accept(message)
            line = (await reader.readline()).decode().rstrip('\n')
            output.publish_batch([
                await update.observed(bridge) for update in notification(
                    line, record_id=transcript_record(str(message.seq))).updates(session)
            ])
            delivered, = await bridge.attempts(message.message_id)
            assert delivered.outcome is DeliveredOutcome

    def replies():
        return tuple((message.target, message.body) for message in comms.bus.bus.message_history()
                     if message.sender == 'viewer' and message.seq > channel.seq)

    final = decoded_record(dict(
        type='assistant', uuid=transcript_record('final'), timestamp='2026-10-29T01:00:00Z',
        message=dict(content=[dict(type='text', text='Private answer for Tristan')]),
    ))
    output.publish_batch(list(final.updates(session)))
    assert replies() == ()
    ended = decoded_record(dict(type='system', subtype='turn_duration'))
    output.publish_batch(list(ended.updates(session)))
    assert replies() == (('claude-code', 'Private answer for Tristan'),)
    output.publish_batch(list(ended.updates(session)))
    assert replies() == (('claude-code', 'Private answer for Tristan'),)
    # A channel remains writable through the explicit send path.
    comms.messaging.send('viewer', '#comms', 'explicit channel update')
    assert replies()[-1] == ('#comms', 'explicit channel update')
