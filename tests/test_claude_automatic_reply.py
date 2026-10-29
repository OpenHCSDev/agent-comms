"""Only a turn's opening DMs receive its final; channels always require explicit sends."""

import pytest

from agent_comms.cli_bridges.claude_protocol import ClaudeSession, KnownTurn
from agent_comms.harness_delivery_states import DeliveredOutcome
from agent_comms.turn_item import NativeTurnId
from transcript_record_fixture import transcript_record
from test_monitor_delivery import attached_monitor, bridge, decoded_record, notification


def assistant_record(label, text):
    return decoded_record(dict(
        type='assistant', uuid=transcript_record(label), timestamp='2026-10-29T01:00:00Z',
        message=dict(content=[dict(type='text', text=text)]),
    ))


async def confirm_messages(bridge, output, session, messages):
    """A real attached monitor and Claude notification feed the ordinary staged output."""
    async with attached_monitor(bridge) as reader:
        for message in messages:
            await bridge.accept(message)
            line = (await reader.readline()).decode().rstrip('\n')
            output.publish_batch([
                await update.observed(bridge) for update in notification(
                    line, record_id=transcript_record(str(message.seq))).updates(session)
            ])
            delivered, = await bridge.attempts(message.message_id)
            assert delivered.outcome is DeliveredOutcome


def replies_after(bridge, sequence):
    return tuple((message.target, message.body)
                 for message in bridge.comms.bus.bus.message_history()
                 if message.sender == bridge.binding.bus and message.seq > sequence)


def finish_reply(output, session):
    ended = decoded_record(dict(type='system', subtype='turn_duration'))
    output.publish_batch(list(ended.updates(session)))


@pytest.mark.asyncio
@pytest.mark.parametrize(('prefix', 'targets'), (
    ((), ('claude-code',)),
    ((assistant_record('already-answering', 'An answer already in progress'),), ()),
    ((decoded_record(dict(
        type='assistant', uuid=transcript_record('already-using-tool'),
        timestamp='2026-10-29T01:00:00Z', message=dict(content=[dict(
            type='tool_use', id='already-running', name='Bash', input=dict(command='pwd'))]),
    )),), ()),
    ((decoded_record(dict(
        type='user', uuid=transcript_record('human-input'), timestamp='2026-10-29T01:00:00Z',
        sessionId='session', promptId='reply-turn', origin=dict(kind='human'),
        message=dict(content='Tristan asks a question'),
    )),), ('claude-code',)),
))
async def test_only_opening_deliveries_receive_the_turns_final(bridge, prefix, targets):
    output = bridge.output_type.for_comms(bridge.comms.transcripts.external('viewer'), bridge.comms)
    session = ClaudeSession(turn=KnownTurn(NativeTurnId('reply-turn')))
    for record in prefix:
        output.publish_batch(list(record.updates(session)))
    direct = bridge.comms.messaging.send('claude-code', 'viewer', 'a delivered DM').message
    await confirm_messages(bridge, output, session, (direct,))
    output.publish_batch(list(assistant_record('final', 'Private answer').updates(session)))
    assert replies_after(bridge, direct.seq) == ()
    finish_reply(output, session)
    expected = tuple((target, 'Private answer') for target in targets)
    assert replies_after(bridge, direct.seq) == expected
    finish_reply(output, session)
    assert replies_after(bridge, direct.seq) == expected

    # Completion opens a fresh window; another sender can open the next turn.
    following = ClaudeSession(turn=KnownTurn(NativeTurnId('following-turn')))
    next_dm = bridge.comms.messaging.send('peer', 'viewer', 'a later opening DM').message
    await confirm_messages(bridge, output, following, (next_dm,))
    output.publish_batch(list(assistant_record('next-final', 'Next answer').updates(following)))
    finish_reply(output, following)
    assert replies_after(bridge, next_dm.seq) == (('peer', 'Next answer'),)


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
    await confirm_messages(bridge, output, session, (channel, direct))
    output.publish_batch(list(assistant_record('final', 'Private answer for Tristan').updates(session)))
    assert replies_after(bridge, channel.seq) == ()
    finish_reply(output, session)
    assert replies_after(bridge, channel.seq) == (('claude-code', 'Private answer for Tristan'),)
    finish_reply(output, session)
    assert replies_after(bridge, channel.seq) == (('claude-code', 'Private answer for Tristan'),)
    # A channel remains writable through the explicit send path.
    comms.messaging.send('viewer', '#comms', 'explicit channel update')
    assert replies_after(bridge, channel.seq)[-1] == ('#comms', 'explicit channel update')
