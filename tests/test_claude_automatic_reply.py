"""Only a turn's opening DMs receive its final; channels always require explicit sends."""

import json
from pathlib import Path

import pytest
from aiohttp import web

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.claude_protocol import ClaudeRecord, ClaudeSession
from agent_comms.cli_bridges.codex import CodexBridge, CodexOptions
from agent_comms.cli_bridges.codex_protocol import ItemStream, RpcNotification
from agent_comms.cli_bridges.shared import Binding, Options, StoredBinding, Watch
from agent_comms.comms import Comms
from agent_comms.connector import ConnectorSendTool
from agent_comms.connector_caller import DirectorCaller
from agent_comms.standing_rules import DirectiveUnacted
from agent_comms.harness_delivery_states import DeliveredOutcome
from agent_comms.messages import AckKind, AlertKind, HandoffKind, InfoKind, Message, MessageKind, NoticeRole, QuestionKind
from agent_comms.channel_targets import ThreadTarget
from agent_comms.repo import Checkout
from agent_comms.turn_digests import LastTurns
from agent_comms.registry_document import RegisteredCommanderProject
from agent_comms.runtime_updates import AllTurnsCancelled, AllTurnsEnded, RuntimeUpdate, ThreadOutput
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


async def replay_records(
    bridge: ClaudeBridge, output: ThreadOutput, records: tuple[ClaudeRecord, ...],
) -> None:
    """A reader restart replays ordered history with a fresh parser, not a rewound live session."""
    session = ClaudeSession()
    for record in records:
        output.publish_batch([await update.observed(bridge) for update in record.updates(session)])


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
    direct = bridge.comms.messaging.send('claude-code', 'viewer', 'a delivered DM', QuestionKind).message
    observations = await confirm_messages(bridge, output, session, (direct,))
    output.publish_batch(list(assistant_record('final', 'Private answer').updates(session)))
    assert replies_after(bridge, direct.seq) == ()
    finish_reply(output, session)
    expected = tuple((target, 'Private answer') for target in targets)
    assert replies_after(bridge, direct.seq) == expected
    finish_reply(output, session)
    assert replies_after(bridge, direct.seq) == expected
    # A subscription rereads retained records, not an input or a provider attempt.
    await replay_records(bridge, output, (
        opening_record('reply-turn'), *prefix, *observations,
        assistant_record('final', 'Private answer'), TURN_FINISHED,
    ))
    assert replies_after(bridge, direct.seq) == expected

    # Completion opens a fresh window; another sender can open the next turn.
    following = session
    next_dm = bridge.comms.messaging.send('peer', 'viewer', 'a later opening DM', QuestionKind).message
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
    channel = comms.messaging.send('peer', '#comms', '@viewer please investigate', QuestionKind).message
    direct = comms.messaging.send('claude-code', 'viewer', 'please answer me privately', QuestionKind).message
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


def assert_opening_send(comms: Comms, opening: Message, later: Message, turn_id: NativeTurnId) -> None:
    unbound = comms.messaging.send('viewer', opening.sender, 'No source identity was supplied').message
    assert unbound.answers == ()
    answered = comms.messaging.send('viewer', opening.sender, 'The opening question is answered',
                                   source_turn=NamedSourceTurn(turn_id)).message
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
    assert_opening_send(comms, opening, later, session.turn.source.require_id())
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
            assert_opening_send(comms, opening, later, NativeTurnId('codex-turn'))
    finally:
        await runner.cleanup()


TURN_FINISHED = decoded_record(dict(type='system', subtype='turn_duration'))
TURN_INTERRUPTED = decoded_record(dict(
    type='user', uuid=transcript_record('interrupted'), timestamp='2026-10-29T01:00:00Z',
    sessionId='session', promptId='directive-turn',
    message=dict(content='[Request interrupted by user]'),
))


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
    ((), (assistant_record('interrupted-output', 'A partial answer'), TURN_INTERRUPTED), False),
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
    for record in (TURN_INTERRUPTED, opening_record('later-source'),
                   assistant_record('later-final', 'A different turn answers')):
        output.publish_batch(list(record.updates(session)))
    finish_reply(output, session)
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
    turns = []
    for prompt_id in ('older-open', 'newer-open'):
        session = ClaudeSession()
        question = bridge.comms.messaging.send('peer', 'viewer', prompt_id, QuestionKind).message
        await confirm_messages(bridge, output, session, (question,), prompt_id=prompt_id)
        turn_id = session.turn.source.require_id()
        turns.append(turn_id)
        assert output.transcript.opening_for(turn_id) == (question.reference,)
    output.publish_batch([ending])
    assert all(output.transcript.opening_for(turn_id) == () for turn_id in turns)


@pytest.mark.asyncio
@pytest.mark.parametrize('retirement', (TURN_FINISHED, TURN_INTERRUPTED), ids=('finished', 'interrupted'))
async def test_same_prompt_id_wake_after_retirement_opens_a_new_turn(
    bridge: ClaudeBridge, retirement: ClaudeRecord,
) -> None:
    comms = bridge.comms
    output = bridge.output_type.for_comms(comms.transcripts.external('viewer'), comms)
    session = ClaudeSession()
    first = comms.messaging.send('peer', 'viewer', 'First source turn', QuestionKind).message
    await confirm_messages(bridge, output, session, (first,), prompt_id='reused-prompt')
    first_id = session.turn.source.require_id()
    assert first_id == NativeTurnId(transcript_record(str(first.seq)))
    output.publish_batch(list(retirement.updates(session)))
    later = comms.messaging.send('peer', 'viewer', 'New wake with reused prompt', QuestionKind).message
    await confirm_messages(bridge, output, session, (later,), prompt_id='reused-prompt')
    later_id = session.turn.source.require_id()
    assert later_id == NativeTurnId(transcript_record(str(later.seq)))
    assert later_id != first_id
    assert output.transcript.opening_for(first_id) == ()
    assert output.transcript.opening_for(later_id) == (later.reference,)
    output.publish_batch(list(assistant_record('reused-final', 'Answer to the new wake').updates(session)))
    finish_reply(output, session)
    final, = (message for message in comms.bus.bus.message_history()
              if message.sender == 'viewer' and message.seq > later.seq)
    assert final.answers == (later.reference,)
    assert replies_after(bridge, later.seq) == (('peer', 'Answer to the new wake'),)
    assert comms.bus.bus.unanswered_references(
        (first.seq, later.seq), frozenset({'viewer'})) == (first.reference,)
    finish_reply(output, session)
    assert replies_after(bridge, later.seq) == (('peer', 'Answer to the new wake'),)


def assert_retained_final(output: ThreadOutput, turn_id: NativeTurnId, text: str) -> None:
    turn, = output.transcript.turns(LastTurns(count=1))
    assert turn.turn_id == turn_id
    assert tuple(part for event in turn.events for part in event.assistant_parts()) == (text,)


@pytest.mark.asyncio
@pytest.mark.parametrize(('kind', 'targets'), (
    (InfoKind, ()), (AckKind, ()),
    (QuestionKind, ('claude-code',)), (HandoffKind, ('claude-code',)),
    (AlertKind, ('claude-code',)),
))
async def test_original_kind_owns_automatic_answer_without_suppressing_the_turn(
    bridge: ClaudeBridge, kind: type[MessageKind], targets: tuple[str, ...],
) -> None:
    comms = bridge.comms
    original = comms.messaging.send('claude-code', 'viewer', 'An original direct message', kind).message
    output = bridge.output_type.for_comms(comms.transcripts.external('viewer'), comms)
    session = ClaudeSession()
    observations = await confirm_messages(bridge, output, session, (original,), prompt_id='kind-turn')
    turn_id = session.turn.source.require_id()
    assert output.transcript.opening_for(turn_id) == (original.reference,)
    output.publish_batch(list(assistant_record('kind-final', 'Holding').updates(session)))
    assert replies_after(bridge, original.seq) == ()
    finish_reply(output, session)
    expected = tuple((target, 'Holding') for target in targets)
    assert replies_after(bridge, original.seq) == expected
    assert_retained_final(output, turn_id, 'Holding')
    sent = tuple(message for message in comms.bus.bus.message_history()
                 if message.sender == 'viewer' and message.seq > original.seq)
    assert tuple(message.answers for message in sent) == tuple((original.reference,) for _ in targets)
    finish_reply(output, session)
    await replay_records(bridge, output, (
        *observations, assistant_record('kind-final', 'Holding'), TURN_FINISHED,
    ))
    assert replies_after(bridge, original.seq) == expected


@pytest.mark.asyncio
async def test_human_info_keeps_its_direct_answer(bridge: ClaudeBridge) -> None:
    comms = bridge.comms
    original = comms.messaging.send_user_message(
        ThreadTarget('viewer'), 'A human input', worktree=Checkout(bridge.options.state.parent))
    assert original.type is InfoKind
    output = bridge.output_type.for_comms(comms.transcripts.external('viewer'), comms)
    session = ClaudeSession()
    await confirm_messages(bridge, output, session, (original,), prompt_id='human-turn')
    turn_id = session.turn.source.require_id()
    assert output.transcript.opening_for(turn_id) == (original.reference,)
    output.publish_batch(list(assistant_record('human-final', 'Human answer').updates(session)))
    finish_reply(output, session)
    assert replies_after(bridge, original.seq) == ((original.sender, 'Human answer'),)
    assert_retained_final(output, turn_id, 'Human answer')


@pytest.mark.asyncio
async def test_notice_question_is_confirmed_without_opening_a_reply(bridge: ClaudeBridge) -> None:
    comms = bridge.comms
    original = comms.messaging.send('peer', 'viewer', 'A question-shaped notice',
                                    QuestionKind, role=NoticeRole()).message
    output = bridge.output_type.for_comms(comms.transcripts.external('viewer'), comms)
    session = ClaudeSession()
    await confirm_messages(bridge, output, session, (original,), prompt_id='notice-turn')
    turn_id = session.turn.source.require_id()
    assert output.transcript.opening_for(turn_id) == ()
    output.publish_batch(list(assistant_record('notice-final', 'Holding').updates(session)))
    finish_reply(output, session)
    assert replies_after(bridge, original.seq) == ()
    assert_retained_final(output, turn_id, 'Holding')


@pytest.mark.asyncio
@pytest.mark.parametrize('information_sender', ('peer', 'claude-code'), ids=('different-sender', 'same-sender'))
async def test_mixed_info_and_question_answer_only_the_asked_sender(
    bridge: ClaudeBridge, information_sender: str,
) -> None:
    comms = bridge.comms
    information = comms.messaging.send(information_sender, 'viewer', 'An informational opening').message
    question = comms.messaging.send('claude-code', 'viewer', 'An asked opening', QuestionKind).message
    output = bridge.output_type.for_comms(comms.transcripts.external('viewer'), comms)
    session = ClaudeSession()
    await confirm_messages(bridge, output, session, (information, question), prompt_id='mixed-turn')
    turn_id = session.turn.source.require_id()
    assert output.transcript.opening_for(turn_id) == (
        information.reference, question.reference)
    output.publish_batch(list(assistant_record('mixed-final', 'One asked answer').updates(session)))
    finish_reply(output, session)
    assert replies_after(bridge, information.seq) == (('claude-code', 'One asked answer'),)
    final, = (message for message in comms.bus.bus.message_history()
              if message.sender == 'viewer' and message.seq > question.seq)
    assert final.answers == (question.reference,)
    assert_retained_final(output, turn_id, 'One asked answer')


@pytest.mark.asyncio
async def test_explicit_answers_and_named_source_sends_can_still_answer_info(bridge: ClaudeBridge) -> None:
    comms = bridge.comms
    information = comms.messaging.send('claude-code', 'viewer', 'An informational opening').message
    output = bridge.output_type.for_comms(comms.transcripts.external('viewer'), comms)
    session = ClaudeSession()
    await confirm_messages(bridge, output, session, (information,), prompt_id='explicit-turn')
    turn_id = session.turn.source.require_id()
    explicit = comms.messaging.send('viewer', 'claude-code', 'Explicit acknowledgement',
                                    answers=(information.reference,)).message
    assert explicit.answers == (information.reference,)
    inferred = comms.messaging.send('viewer', 'claude-code', 'Explicit source acknowledgement',
                                    source_turn=NamedSourceTurn(turn_id)).message
    assert inferred.answers == (information.reference,)
    assert output.transcript.opening_for(turn_id) == (information.reference,)
    output.publish_batch(list(assistant_record('explicit-final', 'Holding').updates(session)))
    finish_reply(output, session)
    assert replies_after(bridge, inferred.seq) == ()
    assert_retained_final(output, turn_id, 'Holding')


@pytest.mark.asyncio
async def test_two_real_bridges_stop_after_the_question_receives_one_info_final(
    bridge: ClaudeBridge, tmp_path: Path,
) -> None:
    comms = bridge.comms
    question = comms.messaging.send('claude-code', 'viewer', 'An asked opening', QuestionKind).message
    output = bridge.output_type.for_comms(comms.transcripts.external('viewer'), comms)
    session = ClaudeSession()
    await confirm_messages(bridge, output, session, (question,), prompt_id='asked-turn')
    turn_id = session.turn.source.require_id()
    assert output.transcript.opening_for(turn_id) == (question.reference,)
    output.publish_batch(list(assistant_record('asked-final', 'The requested answer').updates(session)))
    finish_reply(output, session)
    final, = (message for message in comms.bus.bus.message_history()
              if message.sender == 'viewer' and message.seq > question.seq)
    assert (final.target, final.type, final.answers) == ('claude-code', InfoKind, (question.reference,))
    assert_retained_final(output, turn_id, 'The requested answer')

    state = tmp_path / 'answer-recipient-bridge'
    binding = Binding(root=str(comms.root), owner=comms.registry.require('claude-code').incarnation,
                      thread='session', start=Binding.bus_head(str(comms.root)))
    with Binding.open(state) as db:
        binding.bind(db)
        recipient = ClaudeBridge(Options(Watch(), StoredBinding(), state, state.parent), binding.address(), db)
        await recipient.options.operation.prepare(recipient)
        recipient_output = recipient.output_type.for_comms(comms.transcripts.external('claude-code'), comms)
        recipient_session = ClaudeSession()
        observations = await confirm_messages(recipient, recipient_output, recipient_session,
                                              (final,), prompt_id='info-turn')
        recipient_turn_id = recipient_session.turn.source.require_id()
        assert recipient_output.transcript.opening_for(recipient_turn_id) == (final.reference,)
        recipient_output.publish_batch(list(assistant_record('info-final', 'Holding').updates(recipient_session)))
        finish_reply(recipient_output, recipient_session)
        assert replies_after(recipient, final.seq) == ()
        assert comms.bus.bus.revision() == final.seq
        assert_retained_final(recipient_output, recipient_turn_id, 'Holding')
        finish_reply(recipient_output, recipient_session)
        await replay_records(recipient, recipient_output, (
            *observations, assistant_record('info-final', 'Holding'), TURN_FINISHED,
        ))
        assert comms.bus.bus.revision() == final.seq
