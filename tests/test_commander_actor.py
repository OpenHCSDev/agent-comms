"""Native commander publication uses its explicit actor, never the configured founder."""

import pytest

from agent_comms.commander.order import Commander, SayVerb
from agent_comms.launch_runtime import CallingThreadUnavailable
from agent_comms.messages import QuestionKind
from agent_comms.standing_rules import OwedAnswer
from agent_comms.tool_caller import NoThreadCaller, ThreadCaller
from cli_command import commander

pytest_plugins = ('test_commander_forks',)


@pytest.mark.parametrize('actor,target', [('lead', 'worker'), ('worker', 'lead'), ('worker', '#comms')])
def test_native_commander_posts_as_its_actual_actor(fork_bus, tmp_path, actor, target):
    comms, _, _ = fork_bus
    body = tmp_path / 'message.txt'
    body.write_text('The explicit author owns this publication')
    result = commander('--root', str(comms.root), 'order', 'say', target, str(body),
                       caller=ThreadCaller(actor))
    assert result.returncode == 0, result.stderr
    message = comms.bus.bus.lookup(comms.bus.bus.revision())
    assert message.sender == actor
    assert message.target == target
    assert message.message_id in result.stdout


@pytest.mark.parametrize('caller,extra', [
    (NoThreadCaller(), ()),
    (ThreadCaller('unknown-author'), ()),
    (ThreadCaller('worker'), ('--from', 'lead')),
    (ThreadCaller('worker'), ('--from', 'worker')),
])
def test_native_commander_refuses_missing_unknown_and_duplicate_actor_without_effects(
        fork_bus, tmp_path, caller, extra):
    comms, source, _ = fork_bus
    body = tmp_path / 'message.txt'
    body.write_text('This must never be published')
    before = (comms.bus.bus.revision(), comms.registry.snapshot(),
              comms.goals.todos.list(source.github))
    result = commander('--root', str(comms.root), 'order', 'say', '#comms', str(body),
                       *extra, caller=caller)
    assert result.returncode != 0
    assert (comms.bus.bus.revision(), comms.registry.snapshot(),
            comms.goals.todos.list(source.github)) == before


def test_an_outgoing_question_is_not_the_authors_incoming_debt(fork_bus, tmp_path):
    comms, _, _ = fork_bus
    outgoing = comms.messaging.send('retained', 'lead',
                                   'Root must answer this original alert', type=QuestionKind).message
    assert any(rule.message.reference == outgoing.reference
               for rule in OwedAnswer.opened(comms, 'lead'))
    assert not any(rule.message.reference == outgoing.reference
                   for rule in OwedAnswer.opened(comms, 'retained'))
    body = tmp_path / 'progress.txt'
    body.write_text('The actual author can publish without answering its own outgoing alert')
    result = commander('--root', str(comms.root), 'order', 'say', '#comms', str(body),
                       caller=ThreadCaller('retained'))
    assert result.returncode == 0, result.stderr
    post = comms.bus.bus.lookup(comms.bus.bus.revision())
    assert post.sender == 'retained'
    assert post.answers == ()
    assert any(rule.message.reference == outgoing.reference
               for rule in OwedAnswer.opened(comms, 'lead'))


def test_threadless_operator_read_publishes_nothing(fork_bus):
    comms, _, _ = fork_bus
    before = comms.bus.bus.revision()
    result = commander('--root', str(comms.root), 'order', 'idle', caller=NoThreadCaller())
    assert result.returncode == 0, result.stderr
    assert comms.bus.bus.revision() == before


def test_direct_commander_write_also_requires_its_actual_caller(fork_bus, tmp_path):
    comms, _, _ = fork_bus
    body = tmp_path / 'message.txt'
    body.write_text('A direct verb is not permission to assume the founder')
    before = comms.bus.bus.revision()
    with pytest.raises(CallingThreadUnavailable):
        SayVerb(units='#comms', body_file=str(body)).apply(Commander.read_root(comms.root))
    assert comms.bus.bus.revision() == before
