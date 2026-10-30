"""Native commander publication uses its explicit actor, never the configured founder."""

from dataclasses import replace
import shlex

import pytest

from agent_comms.commander.order import Commander, SayVerb
from agent_comms.launch_runtime import CallingThreadUnavailable
from agent_comms.messages import QuestionKind
from agent_comms.standing_rules import OwedAnswer
from agent_comms.tool_caller import CommanderForkCaller, NoThreadCaller, ThreadCaller
from agent_comms.thread_execution import (
    CommanderForkExecution,
    ExternalThreadExecution,
    NativeThreadExecution,
)
from cli_command import commander

pytest_plugins = ('test_commander_forks',)


@pytest.mark.parametrize('execution', [ExternalThreadExecution, NativeThreadExecution])
@pytest.mark.parametrize('actor,target', [('lead', 'worker'), ('worker', 'lead'), ('worker', '#comms')])
def test_native_commander_posts_as_its_actual_actor(fork_bus, tmp_path, actor, target, execution):
    comms, _, _ = fork_bus
    comms.registry.update_thread(actor, lambda thread: replace(thread, execution=execution))
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


def test_renamed_caller_publishes_its_current_name_without_becoming_the_founder(fork_bus, tmp_path):
    comms, _, _ = fork_bus
    original = comms.registry.require('retained').incarnation
    comms.registry.rename('retained', 'renamed-author')
    renamed = comms.registry.require('renamed-author').incarnation
    assert renamed == original.renamed('renamed-author')
    body = tmp_path / 'renamed-author.txt'
    body.write_text('The retained alias identifies the same author, not the fleet founder')
    result = commander('--root', str(comms.root), 'order', 'say', '#comms', str(body),
                       caller=ThreadCaller('retained'))
    assert result.returncode == 0, result.stderr
    message = comms.bus.bus.lookup(comms.bus.bus.revision())
    assert message.sender == 'renamed-author'
    assert message.sender != 'lead'
    assert message.message_id in result.stdout
    assert comms.registry.require('renamed-author').incarnation == renamed


def test_threadless_operator_read_publishes_nothing(fork_bus):
    comms, _, _ = fork_bus
    before = comms.bus.bus.revision()
    result = commander('--root', str(comms.root), 'order', 'idle', caller=NoThreadCaller())
    assert result.returncode == 0, result.stderr
    assert comms.bus.bus.revision() == before


@pytest.mark.parametrize('caller', [ThreadCaller('lead'), CommanderForkCaller('command-reviewer')])
def test_rendered_commander_command_preserves_root_actor_and_quoted_body(fork_bus, tmp_path, caller):
    comms, _, _ = fork_bus
    comms.registry.register_child('command-reviewer', 'lead', CommanderForkExecution, comms.goals)
    body = tmp_path / 'body with spaces.txt'
    body.write_text('The rendered command publishes from the declared caller')
    command = caller.commander_command(
        comms, *SayVerb.command_arguments('worker', str(body)))
    _, *arguments = shlex.split(command)
    result = commander(*arguments, caller=NoThreadCaller())
    assert result.returncode == 0, result.stderr
    message = comms.bus.bus.lookup(comms.bus.bus.revision())
    assert message.sender == caller.own_thread()
    assert message.target == 'worker'
    assert message.body == body.read_text()


def test_direct_commander_write_also_requires_its_actual_caller(fork_bus, tmp_path):
    comms, _, _ = fork_bus
    body = tmp_path / 'message.txt'
    body.write_text('A direct verb is not permission to assume the founder')
    before = comms.bus.bus.revision()
    with pytest.raises(CallingThreadUnavailable):
        SayVerb(units='#comms', body_file=str(body)).apply(Commander.read_root(comms.root))
    assert comms.bus.bus.revision() == before
