"""Current-format callers never enter the retained amended720 retirement.

The unchanged amended720 controls are preserved at 60fa56ec in Git and exercised
separately with producer 1c5ac5a8, not relabelled as current Thread controls.
"""
import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'tools' / 'cutover'))

import pytest

from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import ClearGoalAction, EditGoalAction, SetGoalAction
from agent_comms.goal_history import GoalHistoryStore
from agent_comms.owner_lifecycle import OwnerReleaseReceipt
from agent_comms.registry_document import RegistryDocument
from agent_comms.threads import Thread
from original_owner_capture import CurrentTypedCapture, OriginalTypedCapture
from owner_read_projection import CurrentThreadProjection, RetiredGoalReportProjection
from thread_format_retirement import GoalReportMemberRetirement
from seed_original_owner_capture import author_model_report

TURN = 'c7cc88d777b947d99b6f28f9e0b6ef97'


def files(root):
    return {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_ino, p.stat().st_mtime_ns)
            for p in root.iterdir() if p.is_file()}


@pytest.mark.parametrize('disposition', ['reported', 'clear', 'replace', 'edit', 'older_release'])
def test_current_projection_preserves_reports_without_retirement(tmp_path, disposition):
    service = Comms(tmp_path / 'current')
    service.registry.register(Thread('author', frozenset(), str(service.root)))
    author_model_report(service, 'author', TURN)
    first = service.registry.require('author')
    release = OwnerReleaseReceipt(1, 2, first)
    if disposition in ('clear', 'replace', 'older_release'):
        service.goals.update_goal(first.name, ClearGoalAction())
    if disposition in ('replace', 'older_release'):
        service.goals.update_goal(first.name, SetGoalAction(text='A distinct current goal'))
    if disposition == 'edit':
        service.goals.update_goal(first.name, EditGoalAction(text='Edited, not a model report'))
    if disposition == 'older_release':
        author_model_report(service, first.name, 'd7cc88d777b947d99b6f28f9e0b6ef97')
    before = files(service.root)
    with service.registry.store.reading() as document:
        history = GoalHistoryStore.acquire_read_only(service.registry.store.path)
        packet = CurrentThreadProjection.project(service.root, service.registry.store.path, document)
        assert packet['document'] == FieldCodec.encode(document)
        assert RegistryDocument.from_wire(packet['document']) == document
    assert any(row.reports_turn(TURN) for row in history)
    assert FieldCodec.decode(OwnerReleaseReceipt, FieldCodec.encode(release)) == release
    assert before == files(service.root)


def test_current_declaration_cannot_acquire_an_old_member_retirement(tmp_path):
    # Reject the selected operation before its journal acquisition or carrier access.
    with pytest.raises(RelationViolationError, match='retained amended720 Thread declaration'):
        GoalReportMemberRetirement.acquire(tmp_path / 'absent.json', RegistryDocument(), {})
    assert not tuple(tmp_path.iterdir())


def test_capture_requires_explicit_source_projection(tmp_path):
    with pytest.raises(TypeError, match='projection'):
        OriginalTypedCapture(tmp_path, Path(sys.executable))
    assert CurrentTypedCapture(tmp_path, Path(sys.executable)).projection is CurrentThreadProjection
    assert OriginalTypedCapture(tmp_path, Path(sys.executable),
                                RetiredGoalReportProjection).projection is RetiredGoalReportProjection


@pytest.mark.parametrize('entry,arguments', [
    ('read_original_owner.py', ('ROOT', 'unused', RetiredGoalReportProjection.declared_name)),
    ('restart_original_thread_format.py', ('0',)),
    ('seed_original_owner_capture.py', ('ROOT',)),
    ('seed_thread_retirement_fixture.py', ('ROOT', 'ROUTE')),
])
def test_old_operation_entrypoints_refuse_current_runtime_before_root_access(tmp_path, entry, arguments):
    root, route = tmp_path / 'must-not-be-created', tmp_path / 'route'
    replacements = {'ROOT': str(root), 'ROUTE': str(route)}
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(SOURCE / 'src')}
    result = subprocess.run([sys.executable, '-B', str(SOURCE / 'tools/cutover' / entry),
                             *(replacements.get(value, value) for value in arguments)],
                            input='', env=environment, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'retained amended720 Thread declaration' in result.stderr
    assert 'AttributeError' not in result.stderr
    assert not root.exists() and not route.exists()
