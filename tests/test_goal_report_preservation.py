"""Authored amended720 report producer -> strict current postimage source controls.

Run against the explicitly amended P source declaration, with this owning tool
family. No installed interpreter, original root, native/provider or live launch.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import ClearGoalAction, EditGoalAction, SetGoalAction
from agent_comms.goal_history import GoalHistoryStore
from agent_comms.owner_lifecycle import OwnerReleaseReceipt
from agent_comms.threads import Thread
from thread_format_retirement import GoalReportMemberRetirement
from seed_original_owner_capture import author_model_report

TARGET_SOURCE = Path(__file__).resolve().parents[1]


def authored_report(root):
    service = Comms(root)
    service.registry.register(Thread('author', frozenset(), str(root)))
    author_model_report(service, 'author', 'c7cc88d777b947d99b6f28f9e0b6ef97')
    return service


def files(root):
    return {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_ino, p.stat().st_mtime_ns)
            for p in root.iterdir() if p.is_file()}


@pytest.mark.parametrize('disposition', ['reported', 'clear', 'replace', 'edit', 'older_release'])
def test_report_survives_goal_replacement_and_strict_target_decodes(tmp_path, disposition):
    service = authored_report(tmp_path / 'authored')
    first = service.registry.require('author')
    releases = {first.name: OwnerReleaseReceipt(1, 2, first)}
    if disposition in ('clear', 'replace', 'older_release'):
        service.goals.update_goal(first.name, ClearGoalAction())
    if disposition in ('replace', 'older_release'):
        service.goals.update_goal(first.name, SetGoalAction(text='A distinct current goal'))
    if disposition == 'edit':
        service.goals.update_goal(first.name, EditGoalAction(text='Edited by owner, not a model report'))
    if disposition == 'older_release':
        author_model_report(service, first.name, 'd7cc88d777b947d99b6f28f9e0b6ef97')
    before = files(service.root)
    with service.registry.store.reading() as document:
        acquired = GoalReportMemberRetirement.acquire(service.registry.store.path, document, releases)
        packet = acquired.project()
    assert acquired.history
    assert any(row.reports_turn(first.last_goal_report_turn) for row in acquired.history)
    assert 'last_goal_report_turn' not in packet['registry']['threads'][first.name]
    assert 'last_goal_report_turn' not in packet['releases'][first.name]['thread']
    # This is an explicit paired SOURCE test, not an installed/runtime overlay.
    environment = dict(os.environ, PYTHONPATH=str(TARGET_SOURCE / 'src'), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([
        sys.executable, '-B', str(TARGET_SOURCE / 'tools/cutover/validate_thread_retirement.py'),
        str(service.registry.store.path),
    ], input=json.dumps(packet), text=True, capture_output=True, env=environment)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['goal_history_rows'] == len(acquired.history)
    assert before == files(service.root)
    # Original full registry continues to refuse the target declaration unchanged.
    negative = subprocess.run([
        sys.executable, '-B', '-c',
        'import json,sys; from agent_comms.registry_document import RegistryDocument; '
        'RegistryDocument.from_wire(json.load(sys.stdin))',
    ], input=json.dumps(FieldCodec.encode(acquired.registry)), text=True,
       capture_output=True, env=environment)
    assert negative.returncode != 0
    assert 'last_goal_report_turn' in negative.stderr


@pytest.mark.parametrize('defect', ['missing_report', 'pending', 'uncertain', 'incarnation', 'gap', 'baseline'])
def test_unproved_report_refuses_before_postimage_without_journal_repair(tmp_path, defect):
    service = authored_report(tmp_path / 'authored')
    with service.registry.store.reading() as document:
        acquired = GoalReportMemberRetirement.acquire(service.registry.store.path, document, {})
    rows = acquired.history
    if defect == 'missing_report':
        rows = rows[:1]
    elif defect in ('pending', 'uncertain'):
        rows = (*rows[:-1], replace(rows[-1], state=defect))
    elif defect == 'incarnation':
        rows = tuple(replace(row, owner_created_at=row.owner_created_at + 1) for row in rows)
    elif defect == 'gap':
        rows = (*rows[:-1], replace(rows[-1], kind='observed_gap'))
    else:
        rows = (replace(rows[-1], kind='baseline', before=None),)
    before = files(service.root)
    # These declared negative witnesses are authored values, never stored rows.
    with pytest.raises(RelationViolationError):
        replace(acquired, history=rows).project()
    assert before == files(service.root)
