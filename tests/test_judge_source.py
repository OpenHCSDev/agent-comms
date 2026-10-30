"""Captured replacement data against real local Git cuts; no live tool call is replayed."""
from dataclasses import replace
import json
import time

import pytest

from agent_comms.commander.judge import Edit, Hunk, JudgeVerb, main
from agent_comms.errors import TodoError
from agent_comms.field_codec import RecordProblems, ToolCallId
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import AcceptedVerdict
from test_judge_dropped import recorded_edit
from source_revision_fixture import SourceRepository

pytest_plugins = ('test_held_problem_orders',)

# The two retained bodies from P927 call6ef11b; their owner and decorators matter.
TARGET = ('@dataclass(frozen=True)\nclass TargetField(ABC):\n'
          '    @property\n    def annotation(self) -> object:\n'
          '        return self.plan.annotation\n\n'
          '    @property\n    def description(self) -> str:\n'
          '        return self.plan.description\n')
ADDED = '\nclass UnsuppliedTargetField(TargetField):\n    value = None\n'


def captured(ctx, name, before, after):
    """An offline captured edit value, not a synthetic publication into the live bus."""
    return Edit(ToolCallId('retained-source-control'), RecordedEventTime(time.time()),
                'worker', str(ctx.repos[0].checkout / name), (Hunk(before, after),), 'P1')


def local_cut(ctx, name, text):
    repo = ctx.repos[0]
    (repo.checkout / name).write_text(text)
    SourceRepository.git(repo.checkout, 'add', '--', name)
    SourceRepository.git(repo.checkout, 'commit', '-m', 'Retained source declaration')
    commit = SourceRepository.git(repo.checkout, 'rev-parse', 'HEAD').strip()
    SourceRepository.git(repo.checkout, 'update-ref', 'refs/remotes/origin/main', commit)
    return commit


def admission(ctx, edit, todo=None):
    todo = ctx.comms.goals.todos.get('P1') if todo is None else todo
    verb = JudgeVerb.parse(['ok', f'{edit.id}:{edit.code()}', '--serves', todo.order.instructions(todo),
                           '--line', edit.hunks[0].new_text.splitlines()[0],
                           '--why', 'The retained owner and actual introduced source were read together'])
    verb.check_edits(ctx, [(todo, edit)])


def test_native_cli_uses_the_whole_write_cut_and_refuses_a_batch_atomically(held_commander):
    ctx = held_commander
    local_cut(ctx, 'target_fields.py', TARGET)
    repo = ctx.repos[0]
    storage = ctx.comms.registry.storage_rule
    allocated = storage.order_checkout(storage.roots_of(ctx.comms.registry.require('worker')), 'P1')
    allocated.parent.mkdir(parents=True, exist_ok=True)
    SourceRepository.git(repo.checkout, 'worktree', 'add', '--detach', str(allocated), 'HEAD')
    (allocated / 'target_fields.py').write_text(TARGET + ADDED)
    (allocated / 'new_file.py').write_text(TARGET)
    recorded_edit(ctx, ToolCallId('retained-source-cli'), str(allocated / 'target_fields.py'), TARGET + ADDED)
    recorded_edit(ctx, ToolCallId('introduced-source-cli'), str(allocated / 'new_file.py'), TARGET)
    edits = {edit.id: edit for edit in Edit.unjudged(ctx.comms, ctx.commander)}
    positive, negative = edits['retained-source-cli'], edits['introduced-source-cli']
    before = ctx.comms.goals.todos.get('P1')
    entries = [{'kind': 'ok', 'ids': [f'{edit.id}:{edit.code()}'],
                'lines': ['class TargetField(ABC):'], 'serves': before.order.instructions(before),
                'why': 'The captured source and its actual inherited declarations were read together'}
               for edit in (positive, negative)]
    batch = ctx.root / 'source-verdict-batch.json'
    batch.write_text(json.dumps(entries))
    with pytest.raises(RecordProblems, match='adds forwarders'):
        main(['batch', str(batch)], ctx)
    assert ctx.comms.goals.todos.get('P1') == before
    assert {positive.id, negative.id} <= {edit.id for edit in Edit.unjudged(ctx.comms, ctx.commander)}
    main(['ok', f'{positive.id}:{positive.code()}', '--line', 'class TargetField(ABC):',
          '--serves', before.order.instructions(before), '--why', entries[0]['why']], ctx)
    changed = ctx.comms.goals.todos.get('P1')
    assert any(verdict.tool_call_id == positive.id
               for verdict in changed.history.events(changed.id, AcceptedVerdict))
    assert {edit.id for edit in Edit.unjudged(ctx.comms, ctx.commander)} >= {negative.id}


def test_whole_write_uses_exact_git_baseline_not_mutable_file(held_commander):
    ctx = held_commander
    local_cut(ctx, 'target_fields.py', TARGET)
    edit = captured(ctx, 'target_fields.py', '', TARGET + ADDED)
    # The working file is deliberately no longer evidence of the retained methods.
    (ctx.repos[0].checkout / 'target_fields.py').write_text('not the preimage\n')
    admission(ctx, edit)
    assert ctx.comms.goals.todos.get('P1').revision > 0


@pytest.mark.parametrize('name,before,after', (
    ('new_file.py', '', TARGET),
    ('target_fields.py', '', TARGET.replace('self.plan.annotation', 'self.other.annotation')),
    ('target_fields.py', TARGET, TARGET.replace('self.plan.description', 'self.other.description')),
))
def test_real_new_or_changed_forwarder_is_not_retained(held_commander, name, before, after):
    ctx = held_commander
    local_cut(ctx, 'target_fields.py', TARGET)
    original = ctx.comms.goals.todos.get('P1')
    with pytest.raises(RecordProblems, match='adds forwarders'):
        admission(ctx, captured(ctx, name, before, after))
    assert ctx.comms.goals.todos.get('P1') == original


def test_missing_or_foreign_whole_file_evidence_refuses_without_mutation(held_commander):
    ctx = held_commander
    local_cut(ctx, 'target_fields.py', TARGET)
    original = ctx.comms.goals.todos.get('P1')
    edit = captured(ctx, 'target_fields.py', '', TARGET + ADDED)
    with pytest.raises(TodoError, match='no checkout of a registered repository'):
        admission(ctx, replace(edit, path=str(ctx.root / 'foreign.py')))
    repo = ctx.repos[0]
    SourceRepository.git(repo.checkout, 'update-ref', '-d', 'refs/remotes/origin/main')
    with pytest.raises(RecordProblems, match='source evidence unavailable'):
        admission(ctx, captured(ctx, 'target_fields.py', '', TARGET + ADDED))
    assert ctx.comms.goals.todos.get('P1') == original


@pytest.mark.parametrize('before,after', (
    ('class Unfinished:', 'class Unfinished:\n    extra = 1\nclass Next:'),
    ('class Unfinished:', 'harmless = 1'),
))
def test_missing_fragment_declarations_refuse_even_without_textual_forwarders(
        held_commander, before, after):
    ctx = held_commander
    local_cut(ctx, 'target_fields.py', TARGET)
    original = ctx.comms.goals.todos.get('P1')
    edit = captured(ctx, 'target_fields.py', before, after)
    with pytest.raises(RecordProblems, match='retained-source-control'):
        admission(ctx, edit)
    assert ctx.comms.goals.todos.get('P1') == original


def test_captured_replacements_do_not_need_main_or_unique_occurrence(held_commander):
    ctx = held_commander
    repo = ctx.repos[0]
    local_cut(ctx, 'target_fields.py', 'class MainOnly:\n    pass\n')
    SourceRepository.git(repo.checkout, 'update-ref', '-d', 'refs/remotes/origin/main')
    # Neither branch-only source nor repeated captured text requires a main preimage.
    (repo.checkout / 'target_fields.py').write_text(TARGET + TARGET)
    edit = captured(ctx, 'target_fields.py', TARGET, TARGET + ADDED)
    original = ctx.comms.goals.todos.get('P1')
    admission(ctx, edit)
    admission(ctx, replace(edit, hunks=(Hunk(TARGET, TARGET + ADDED),
                                      Hunk(TARGET, TARGET + ADDED + '\nextra = 1\n'))))
    assert ctx.comms.goals.todos.get('P1') == original
