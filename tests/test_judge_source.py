"""Captured replacement data against real local Git cuts; no live tool call is replayed."""
from dataclasses import replace
import time

import pytest

from agent_comms.change_report import PullRequestHeadRevision
from agent_comms.commander.judge import Edit, Hunk, JudgeVerb
from agent_comms.errors import MissingSourceLine, TodoError
from agent_comms.field_codec import RecordProblems, ToolCallId
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import PullRequestLink, RecordPullRequest
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


def test_whole_write_uses_exact_git_baseline_not_mutable_file(held_commander, capsys):
    ctx = held_commander
    commit = local_cut(ctx, 'target_fields.py', TARGET)
    edit = captured(ctx, 'target_fields.py', '', TARGET + ADDED)
    # The working file is deliberately no longer evidence of the retained methods.
    (ctx.repos[0].checkout / 'target_fields.py').write_text('not the preimage\n')
    admission(ctx, edit)
    assert f'{commit}:target_fields.py' in capsys.readouterr().out
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


def test_missing_foreign_and_stale_source_evidence_refuse_without_mutation(held_commander):
    ctx = held_commander
    local_cut(ctx, 'target_fields.py', TARGET)
    original = ctx.comms.goals.todos.get('P1')
    edit = captured(ctx, 'target_fields.py', 'not the original captured text', TARGET)
    with pytest.raises(MissingSourceLine, match='source evidence unavailable'):
        admission(ctx, edit)
    with pytest.raises(TodoError, match='no checkout of a registered repository'):
        admission(ctx, replace(edit, path=str(ctx.root / 'foreign.py')))
    repo = ctx.repos[0]
    SourceRepository.git(repo.checkout, 'update-ref', '-d', 'refs/remotes/origin/main')
    with pytest.raises(MissingSourceLine, match='source evidence unavailable'):
        admission(ctx, captured(ctx, 'target_fields.py', '', TARGET + ADDED))
    assert ctx.comms.goals.todos.get('P1') == original


def test_old_text_can_select_its_qualified_local_carrier_without_fetch(held_commander):
    ctx = held_commander
    repo = ctx.repos[0]
    local_cut(ctx, 'target_fields.py', 'class MainOnly:\n    pass\n')
    before = TARGET
    (repo.checkout / 'target_fields.py').write_text(before)
    SourceRepository.git(repo.checkout, 'add', '--', 'target_fields.py')
    SourceRepository.git(repo.checkout, 'commit', '-m', 'Unmerged carrier declaration')
    commit = SourceRepository.git(repo.checkout, 'rev-parse', 'HEAD').strip()
    todo = ctx.comms.goals.todos.get('P1')
    held = todo.held()
    link = PullRequestLink(repo.github, 7)
    RecordPullRequest(todo.revision, held.owner_identity, link, held.generation).record(
        ctx.comms.goals, todo.id)
    todo = ctx.comms.goals.todos.get(todo.id)
    carrier = PullRequestHeadRevision(link)
    SourceRepository.git(repo.checkout, 'update-ref', carrier.reference, commit)
    edit = captured(ctx, 'target_fields.py', before, before + ADDED)
    admission(ctx, edit)
    # The same ref number in an undeclared repository gives no source authority.
    without = replace(todo, order=replace(todo.order, pull_requests=()))
    with pytest.raises(MissingSourceLine, match='source evidence unavailable'):
        admission(ctx, edit, without)
