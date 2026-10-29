"""The AST's declarations and the actual commander JSON/command boundary."""

from dataclasses import replace
import json
from pathlib import Path
import time

import pytest

from agent_comms.commander.order import OrderFile, OverrideNames, forwarders_added, main, overrides
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


def test_generic_base_identity_excludes_its_type_arguments():
    source = ('class DecoderPlan:\n    def answer(self): pass\n'
              'class ValueDecoder[T](DecoderPlan):\n    pass\n'
              'class UnionDecoder[T](ValueDecoder[T]):\n    pass\n'
              'class Represented[T](ValueDecoder[T]):\n'
              '    def answer(self): return self.peer.answer()\n'
              'class TextEdited[T](UnionDecoder[T]):\n'
              '    def answer(self): return self.peer.answer()\n')
    assert overrides((source,)) == {'answer'}
    assert not forwarders_added('+    def answer(self): return self.peer.answer()\n', (source,))
    unrelated = ('class Unrelated:\n    def answer(self): pass\n'
                 'class Parent[T]:\n    pass\n'
                 'class Child(Parent[Unrelated]):\n'
                 '    def answer(self): return self.peer.answer()\n')
    assert not overrides((unrelated,))
    assert forwarders_added('+    def answer(self): return self.peer.answer()\n', (unrelated,))


def test_retained_generic_decoder_fragments_use_the_actual_after_declarations():
    from agent_comms.commander.judge import Hunk

    parents = ('class DecoderPlan:\n    editor_annotation: object\n'
               '    def format_editor(self, wire): pass\n'
               '    def parse_editor(self, text): pass\n'
               'class ValueDecoder[T](DecoderPlan):\n    pass\n'
               'class UnionDecoder[T](ValueDecoder[T]):\n    pass\n')
    represented = ('class _RepresentedDecoder[T](ValueDecoder[T]):\n'
                   '    @property\n    def editor_annotation(self) -> object:\n'
                   '        return self.wire_structure.editor_annotation\n'
                   '    def format_editor(self, wire: object) -> JsonValue:\n'
                   '        return self.wire_structure.format_editor(wire)\n'
                   '    def parse_editor(self, text: object) -> object:\n'
                   '        return self.wire_structure.parse_editor(text)\n')
    text_edited = ('@dataclass(frozen=True, slots=True)\n'
                   'class _TextEditedUnionDecoder[T](UnionDecoder[T]):\n'
                   '    editor: DecoderPlan\n'
                   '    def format_editor(self, wire: object) -> JsonValue:\n'
                   '        return self.editor.format_editor(wire)\n'
                   '    def parse_editor(self, text: object) -> object:\n'
                   '        return self.editor.parse_editor(text)\n')
    # call94d8's final hunk ends at the next class header; it is not a whole module.
    tail = '@dataclass(frozen=True, slots=True)\nclass _ArrayDecoder[T: Iterable[object]](ValueDecoder[T]):\n'
    after = parents + represented + text_edited + tail + '    pass\n'
    assert Hunk(tail, text_edited + '\n' + tail).introduced_forwarders((after,)) == []
    assert not forwarders_added(OverrideNames.introduced(parents, parents + represented),
                                (parents + represented,))


def test_retained_replacement_methods_keep_owner_decorators_and_body_identity():
    # P952 call641730 retained binding_mode while inserting key_mode_label.
    before = ('    @property\n'
              '    def binding_mode(self) -> type[EditingMode]:\n'
              '        return self.editing_mode.binding_mode\n')
    after = ('    @property\n    def key_mode_label(self) -> str:\n'
             '        return self.editing_mode.label\n\n' + before)
    assert not forwarders_added(OverrideNames.introduced(before, after))
    changed = before.replace('self.editing_mode', 'self.other_mode')
    assert forwarders_added(OverrideNames.introduced(before, changed))
    assert forwarders_added(OverrideNames.introduced('', before))
    owner = 'class Original:\n' + before
    moved = 'class Different:\n' + before
    assert forwarders_added(OverrideNames.introduced(owner, moved))
    assert forwarders_added(OverrideNames.introduced(before, before.replace('@property', '@different')))


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
        check=OrderCheck(ctx.repos[0].checkout, 'false'),
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
            assert created.holder == 'worker' and created.strike_count == 0
            document = replace(document, problems=(created.id,))
            original_message = created.latest_message
        else:
            main(['send', 'worker', str(path), '--clean'], ctx)
            resent, = ctx.todos()
            assert resent.holder == 'worker' and resent.strike_count == 0
            assert resent.latest_message != original_message
            main(['send', 'worker', str(path), '--reassign',
                  'The old attempt failed before recording the required source change'], ctx)
            assert ctx.comms.goals.todos.get(created.id).strike_count == 0
            main(['send', 'worker', str(path)], ctx)
            assert ctx.comms.goals.todos.get(created.id).strike_count == 1
            main(['send', 'worker', str(path)], ctx)
            assert ctx.comms.goals.todos.get(created.id).strike_count == 1
