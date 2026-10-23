"""The AST's declarations and the actual commander JSON/command boundary."""

from dataclasses import replace
import json
from pathlib import Path
import time

import pytest

from agent_comms.commander.order import OrderFile, forwarders_added, main, overrides
from agent_comms.commander.request import RequestAddVerb
from agent_comms.field_codec import FieldCodec
from agent_comms.goals import OrderCheck
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repo import Repo
from test_todos import order, thread

pytest_plugins = ("test_todos",)


@pytest.mark.parametrize('definition', [
    'def answer(self): return self.peer.answer()',
    'async def answer(self): return self.peer.answer()',
    'answer = 1',
    'answer: int = 1',
])
def test_override_scan_follows_direct_declarations_through_multiple_parents(definition):
    source = ('class Root:\n    ' + definition + '\n'
              'class Left(Root):\n    pass\n'
              'class Right:\n    pass\n'
              'class Leaf(Left, Right):\n    def answer(self): return self.peer.answer()\n')
    assert overrides((source,)) == {'answer'}
    diff = '+    def answer(self): return self.peer.answer()\n'
    assert forwarders_added(diff, (source,)) == []
    assert forwarders_added(diff, ())


def test_override_scan_excludes_nested_and_qualified_names():
    source = ('class Root:\n'
              '    def other(self):\n        def answer(): pass\n'
              '    field.answer: int = 1\n'
              '    first, answer = (1, 2)\n'
              'class Leaf(package.Root):\n'
              '    def answer(self): return self.peer.answer()\n')
    assert overrides((source, 'class malformed(')) == set()
    assert forwarders_added('+    def answer(self): return self.peer.answer()\n', (source,))


def test_problem_and_send_share_field_refusals_and_record_only_valid_documents(pr_commander):
    ctx = pr_commander.model_copy(update={
        'repos': (*pr_commander.repos, Repo.of(Path(__file__).resolve().parents[1])),
    })
    ctx.comms.registry.register(thread('worker', ctx.root, 2.0))
    ctx.comms.channels.create_tag('comms')
    RequestAddVerb(text='Repair the copied order workflow without changing original retained state').run(ctx)
    now = time.time()
    planned = replace(order(ctx.repos[0]),
        check=OrderCheck(str(ctx.repos[0].checkout), 'false'),
        at=RecordedEventTime(now), first_call_deadline=RecordedEventTime(now + 120))
    document = OrderFile(planned,
        'The copied workflow still contains an unfinished decision that prevents completion',
        'This is stupid because a stale decision prevents the existing owner from finishing',
        unplanned_reason='This isolated workflow verifies order recording rather than advancing the production plan.')
    path = ctx.root / 'order.json'
    wrong_fields = (
        (('defect',), 'short', 'defect'),
        (('why_stupid',), '', 'why_stupid'),
        (('order', 'scope'), {'kind': 'fix', 'shape': {'parents': [], 'members': []}}, 'parents'),
        (('order', 'check', 'command'), 'git checkout main', 'checkout'),
        (('order', 'edits'), [], 'edits'),
        (('order', 'banned'), [], 'banned'),
        (('order', 'report'), '', 'order text'),
        (('order', 'run'), {'kind': 'not-a-run'}, 'not-a-run'),
        (('slop',), [{'pattern': 'NOT-A-PATTERN'}], 'slop'),
    )
    for command in ('problem', 'send'):
        for fields, value, error in wrong_fields:
            payload = FieldCodec.encode(document)
            target = payload
            for field in fields[:-1]:
                target = target[field]
            target[fields[-1]] = value
            path.write_text(json.dumps(payload))
            before = tuple(ctx.todos())
            args = (['problem', str(path), '--to', 'worker', '--where', planned.owner.to_wire()]
                    if command == 'problem' else ['send', 'worker', str(path), '--clean'])
            with pytest.raises(SystemExit, match=error):
                main(args, ctx)
            assert tuple(ctx.todos()) == before
        path.write_text(json.dumps(FieldCodec.encode(document)))
        if command == 'problem':
            main(['problem', str(path), '--to', 'worker', '--where', planned.owner.to_wire()], ctx)
            created, = ctx.todos()
            assert created.holder == 'worker' and not created.strikes
            document = replace(document, problems=(created.id,))
            original_message = created.messages[-1]
        else:
            main(['send', 'worker', str(path), '--clean'], ctx)
            resent, = ctx.todos()
            assert resent.holder == 'worker' and not resent.strikes
            assert resent.messages[-1] != original_message
            main(['send', 'worker', str(path), '--reassign',
                  'The old attempt failed before recording the required source change'], ctx)
            assert not ctx.comms.goals.todos.get(created.id).strikes
            main(['send', 'worker', str(path)], ctx)
            assert len(ctx.comms.goals.todos.get(created.id).strikes) == 1
            main(['send', 'worker', str(path)], ctx)
            assert len(ctx.comms.goals.todos.get(created.id).strikes) == 1
