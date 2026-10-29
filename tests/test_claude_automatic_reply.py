"""Only a turn's opening DMs receive its final; channels always require explicit sends."""

import json
from pathlib import Path

import pytest
from aiohttp import web

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.claude_protocol import ClaudeRecord, ClaudeSession
from agent_comms.cli_bridges.codex import CodexBridge, CodexOptions
from agent_comms.cli_bridges.codex_protocol import ItemStream, RpcNotification
from agent_comms.cli_bridges.shared import Binding, StoredBinding, Watch
from agent_comms.comms import Comms
from agent_comms.connector import ConnectorSendTool
from agent_comms.connector_caller import DirectorCaller
from agent_comms.standing_rules import DirectiveUnacted
from agent_comms.harness_delivery_states import DeliveredOutcome
from agent_comms.messages import Message, QuestionKind
from agent_comms.registry_document import RegisteredCommanderProject
from agent_comms.errors import RelationViolationError
from agent_comms.runtime_updates import AllTurnsCancelled, AllTurnsEnded, FinishedTurn, RuntimeUpdate, ThreadOutput
from agent_comms.turn_item import NamedSourceTurn, NativeTurnId
from transcript_record_fixture import transcript_record
from test_monitor_delivery import attached_monitor, bridge, decoded_record, notification


def assistant_record(label: str, text: str) -> ClaudeRecord:
    return decoded_record(dict(
        type='assistant', uuid=transcript_record(label), timestamp='2026-10-29T01:00:00Z',
        message=dict(content=[dict(type='text', text=text)]),
    ))


def opening_record(prompt_id: str) -> ClaudeRecord:
    return decoded_record(dict(
        type='user', uuid=transcript_record(prompt_id), timestamp='2026-10-29T01:00:00Z',
        sessionId='session', promptId=prompt_id, origin=dict(kind='human'),
        message=dict(content='An earlier user turn'),
    ))


async def confirm_messages(
    bridge: ClaudeBridge, output: ThreadOutput, session: ClaudeSession, messages: tuple[Message, ...],
    *, prompt_id: str = 'reply-turn',
) -> tuple[ClaudeRecord, ...]:
    """A real attached monitor and Claude notification feed the ordinary staged output."""
    observations: list[ClaudeRecord] = []
    async with attached_monitor(bridge) as reader:
        for message in messages:
            await bridge.accept(message)
            line = (await reader.readline()).decode().rstrip('\n')
            record = notification(line, record_id=transcript_record(str(message.seq)), prompt_id=prompt_id)
            observations.append(record)
            output.publish_batch([await update.observed(bridge) for update in record.updates(session)])
            delivered, = await bridge.attempts(message.message_id)
            assert delivered.outcome is DeliveredOutcome
    return tuple(observations)


def replies_after(bridge: ClaudeBridge, sequence: int) -> tuple[tuple[str, str], ...]:
    return tuple((message.target, message.body)
                 for message in bridge.comms.bus.bus.message_history()
                 if message.sender == bridge.binding.bus and message.seq > sequence)


def finish_reply(output: ThreadOutput, session: ClaudeSession) -> None:
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
async def test_only_opening_deliveries_receive_the_turns_final(
    bridge: ClaudeBridge, prefix: tuple[ClaudeRecord, ...], targets: tuple[str, ...],
) -> None:
    output = bridge.output_type.for_comms(bridge.comms.transcripts.external('viewer'), bridge.comms)
    session = ClaudeSession()
    output.publish_batch(list(opening_record('reply-turn').updates(session)))
    for record in prefix:
        output.publish_batch(list(record.updates(session)))
    direct = bridge.comms.messaging.send('claude-code', 'viewer', 'a delivered DM').message
    observations = await confirm_messages(bridge, output, session, (direct,))
    output.publish_batch(list(assistant_record('final', 'Private answer').updates(session)))
    assert replies_after(bridge, direct.seq) == ()
    finish_reply(output, session)
    expected = tuple((target, 'Private answer') for target in targets)
    assert replies_after(bridge, direct.seq) == expected
    finish_reply(output, session)
    assert replies_after(bridge, direct.seq) == expected
    # A subscription rereads retained records, not an input or a provider attempt.
    for record in observations:
        output.publish_batch([await update.observed(bridge) for update in record.updates(session)])
    assert replies_after(bridge, direct.seq) == expected

    # Completion opens a fresh window; another sender can open the next turn.
    following = session
    next_dm = bridge.comms.messaging.send('peer', 'viewer', 'a later opening DM').message
    await confirm_messages(bridge, output, following, (next_dm,), prompt_id='following-turn')
    output.publish_batch(list(assistant_record('next-final', 'Next answer').updates(following)))
    finish_reply(output, following)
    assert replies_after(bridge, next_dm.seq) == (('peer', 'Next answer'),)


@pytest.mark.asyncio
async def test_channel_mention_and_dm_share_a_turn_without_broadcasting_the_final(
    bridge: ClaudeBridge,
) -> None:
    comms = bridge.comms
    comms.channels.create_tag('comms')
    for name in ('claude-code', 'viewer', 'peer'):
        comms.channels.update_tags(name, add=frozenset({'comms'}))
    channel = comms.messaging.send('peer', '#comms', '@viewer please investigate').message
    direct = comms.messaging.send('claude-code', 'viewer', 'please answer me privately').message
    output = bridge.output_type.for_comms(comms.transcripts.external('viewer'), comms)
    session = ClaudeSession()
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


def assert_opening_send(comms: Comms, opening: Message, later: Message, turn_id: str) -> None:
    unbound = comms.messaging.send('viewer', opening.sender, 'No source identity was supplied').message
    assert unbound.answers == ()
    answered = comms.messaging.send('viewer', opening.sender, 'The opening question is answered',
                                   source_turn=NamedSourceTurn(NativeTurnId(turn_id))).message
    assert answered.answers == (opening.reference,)
    unrelated = comms.messaging.send('viewer', later.sender, 'A separate status update').message
    assert unrelated.answers == ()
    assert comms.bus.bus.unanswered_references(
        (opening.seq, later.seq), frozenset({'viewer'})) == (later.reference,)


@pytest.mark.asyncio
async def test_commander_send_answers_only_the_question_that_opened_its_turn(bridge: ClaudeBridge) -> None:
    comms = bridge.comms
    commander = comms.registry.require('viewer')
    comms.registry.set_commander_project(RegisteredCommanderProject(commander.incarnation, ()))
    output = bridge.output_type.for_comms(comms.transcripts.external('viewer'), comms)
    session = ClaudeSession()
    opening = comms.messaging.send('peer', 'viewer', 'Please investigate', QuestionKind).message
    await confirm_messages(bridge, output, session, (opening,), prompt_id='commander-turn')
    output.publish_batch(list(assistant_record('commander-output', 'Investigating').updates(session)))
    later = comms.messaging.send('claude-code', 'viewer', 'Another question', QuestionKind).message
    await confirm_messages(bridge, output, session, (later,), prompt_id='commander-turn')
    assert_opening_send(comms, opening, later, 'commander-turn')
    finish_reply(output, session)
    outside = comms.messaging.send('viewer', 'claude-code', 'Outside that completed turn').message
    assert outside.answers == ()


@pytest.mark.asyncio
async def test_codex_send_answers_only_the_question_that_opened_its_turn(
    bridge: ClaudeBridge, tmp_path: Path,
) -> None:
    starts: list[dict] = []

    async def serve(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            frame = json.loads(message.data)
            method = frame['method']
            if method == 'initialized':
                continue
            if method == 'initialize':
                result = {'userAgent': 'codex/0.0.0'}
            else:
                assert method == 'turn/start'
                starts.append(frame['params'])
                result = {}
            await socket.send_json({'id': frame['id'], 'result': result})
        return socket

    app = web.Application()
    app.router.add_get('/', serve)
    runner = web.AppRunner(app)
    await runner.setup()
    socket_path = tmp_path.parent / 'opening-codex.sock'
    await web.UnixSite(runner, str(socket_path)).start()
    comms = bridge.comms
    state = tmp_path / 'codex-bridge'
    binding = Binding(root=str(comms.root), owner=comms.registry.require('viewer').incarnation,
                      thread='codex-session', start=Binding.bus_head(str(comms.root)))
    try:
        with Binding.open(state) as db:
            binding.bind(db)
            options = CodexOptions(Watch(), StoredBinding(), state, state.parent, str(socket_path))
            codex = CodexBridge(options, binding.address(), db)
            await options.operation.prepare(codex)
            output = codex.output_type.for_comms(comms.transcripts.external('viewer'), comms)
            stream = ItemStream('codex-session')

            async def receive(item: dict) -> None:
                updates = stream.receive(RpcNotification(method='item/completed', params=dict(
                    threadId='codex-session', turnId='codex-turn', item=item,
                    completedAtMs=1761699600000)))
                output.publish_batch([await update.observed(codex) for update in updates])

            async def deliver(original: Message) -> None:
                await codex.accept(original)
                sent = starts[-1]
                await receive(dict(type='userMessage', id=f'input-{original.seq}',
                                   content=sent['input'], clientId=sent['clientUserMessageId']))

            opening = comms.messaging.send('peer', 'viewer', 'Please investigate', QuestionKind).message
            await deliver(opening)
            await receive(dict(type='agentMessage', id='working', text='Investigating'))
            later = comms.messaging.send('claude-code', 'viewer', 'Another question', QuestionKind).message
            await deliver(later)
            assert_opening_send(comms, opening, later, 'codex-turn')
    finally:
        await runner.cleanup()


TURN_FINISHED = decoded_record(dict(type='system', subtype='turn_duration'))


@pytest.mark.asyncio
@pytest.mark.parametrize(('prefix', 'suffix', 'cleared'), (
    ((), (assistant_record('directive-final', 'The requested work is explained'), TURN_FINISHED), True),
    ((), (decoded_record(dict(
        type='assistant', uuid=transcript_record('thinking-only'), timestamp='2026-10-29T01:00:00Z',
        message=dict(content=[dict(type='thinking', thinking='Still considering the work')]),
    )), TURN_FINISHED), False),
    ((), (assistant_record('not-finished', 'An unfinished answer'),), False),
    ((opening_record('directive-turn'), assistant_record('before-directives', 'Already answering something else')),
     (assistant_record('following-directives', 'Finishing that older answer'), TURN_FINISHED), False),
    ((), (assistant_record('interrupted-output', 'A partial answer'), decoded_record(dict(
        type='user', uuid=transcript_record('interrupted'), timestamp='2026-10-29T01:00:00Z',
        sessionId='session', promptId='directive-turn',
        message=dict(content='[Request interrupted by user]'),
    ))), False),
    ((), (assistant_record('empty-final', ''), TURN_FINISHED), False),
), ids=('committed-final', 'thinking-only', 'still-open', 'mid-output', 'interrupted', 'empty-final'))
async def test_twelve_directives_require_the_final_of_the_turn_they_opened(
    bridge: ClaudeBridge, prefix: tuple[ClaudeRecord, ...], suffix: tuple[ClaudeRecord, ...], cleared: bool,
) -> None:
    comms = bridge.comms
    commander = comms.registry.require('viewer')
    comms.registry.set_commander_project(RegisteredCommanderProject(commander.incarnation, ()))
    output = bridge.output_type.for_comms(comms.transcripts.external('viewer'), comms)
    session = ClaudeSession()
    output.publish_batch(list(opening_record('previous-turn').updates(session)))
    finish_reply(output, session)
    for record in prefix:
        output.publish_batch(list(record.updates(session)))
    directives = []
    for number in range(12):
        request = comms.goals.todos.add_request(commander.incarnation, f'Independent request {number}')
        ConnectorSendTool(target='viewer', request=request.id,
                          body=f'Explain independent request {number}.').apply(
                              comms, DirectorCaller('claude-code'))
        directive, = comms.goals.todos.owner_request(request.id).directives
        with comms.bus.bus.reading() as source:
            directives.append(directive.original(source))
    originals = tuple(directives)
    assert {rule.message.reference for rule in DirectiveUnacted.opened(comms, 'viewer')} == {
        message.reference for message in originals}
    await confirm_messages(bridge, output, session, originals, prompt_id='directive-turn')
    before = comms.bus.bus.revision()
    for record in suffix:
        output.publish_batch(list(record.updates(session)))
    expected = set() if cleared else {message.reference for message in originals}
    assert {rule.message.reference for rule in DirectiveUnacted.opened(comms, 'viewer')} == expected
    assert comms.bus.bus.revision() == before
    for record in (opening_record('later-source'), assistant_record('later-final', 'A different turn answers')):
        output.publish_batch(list(record.updates(session)))
    output.publish_batch([FinishedTurn(NativeTurnId('later-source'))])
    assert {rule.message.reference for rule in DirectiveUnacted.opened(comms, 'viewer')} == expected
    assert comms.bus.bus.unanswered_references(
        tuple(message.seq for message in originals), frozenset({'viewer'})) == tuple(
            message.reference for message in originals)


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', (AllTurnsEnded(), AllTurnsCancelled()), ids=('finish', 'cancel'))
async def test_retirement_visits_all_live_windows_not_the_newest_row(
    bridge: ClaudeBridge, ending: RuntimeUpdate,
) -> None:
    output = bridge.output_type.for_comms(bridge.comms.transcripts.external('viewer'), bridge.comms)
    session = ClaudeSession()
    for prompt_id in ('older-open', 'newer-open'):
        question = bridge.comms.messaging.send('peer', 'viewer', prompt_id, QuestionKind).message
        await confirm_messages(bridge, output, session, (question,), prompt_id=prompt_id)
        assert output.transcript.opening_for(NativeTurnId(prompt_id)) == (question.reference,)
    output.publish_batch([ending])
    assert output.transcript.opening_for(NativeTurnId('older-open')) == ()
    assert output.transcript.opening_for(NativeTurnId('newer-open')) == ()


@pytest.mark.asyncio
async def test_same_prompt_id_wake_after_retirement_refuses_instead_of_dropping(
    bridge: ClaudeBridge,
) -> None:
    output = bridge.output_type.for_comms(bridge.comms.transcripts.external('viewer'), bridge.comms)
    session = ClaudeSession()
    first = bridge.comms.messaging.send('peer', 'viewer', 'First source turn').message
    await confirm_messages(bridge, output, session, (first,), prompt_id='reused-prompt')
    finish_reply(output, session)
    later = bridge.comms.messaging.send('peer', 'viewer', 'New wake with reused prompt', QuestionKind).message
    with pytest.raises(RelationViolationError, match='reused-prompt'):
        await confirm_messages(bridge, output, session, (later,), prompt_id='reused-prompt')
    assert output.transcript.opening_for(NativeTurnId('reused-prompt')) == ()
    assert bridge.comms.bus.bus.unanswered_references((later.seq,), frozenset({'viewer'})) == (later.reference,)
