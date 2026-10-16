"""Current typed reads preserve authored reports and reject invalid read modes."""
import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'tools' / 'cutover'))

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import ClearGoalAction, EditGoalAction, SetGoalAction
from agent_comms.goal_history import GoalHistoryStore
from agent_comms.owner_lifecycle import OwnerReleaseReceipt
from agent_comms.registry_document import RegistryDocument
from agent_comms.threads import Thread
from owner_read_projection import CurrentThreadProjection
from seed_original_owner_capture import author_model_report

TURN = 'c7cc88d777b947d99b6f28f9e0b6ef97'


def files(root):
    return {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_ino, p.stat().st_mtime_ns)
            for p in root.iterdir() if p.is_file()}


@pytest.mark.parametrize('disposition', ['reported', 'clear', 'replace', 'edit', 'older_release'])
def test_current_projection_preserves_authored_reports(tmp_path, disposition):
    service = Comms(tmp_path / 'current')
    service.registry.register(Thread('author', frozenset(), str(service.root),
                                    process_identity=ProcessIdentity.capture(os.getpid())),
                              new_owner=True)
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
    packet = CurrentThreadProjection.read(service.registry, service.root, first.name, None)
    with service.registry.store.reading() as document:
        history = GoalHistoryStore.acquire_read_only(service.registry.store.path)
        assert packet.document == document
        assert RegistryDocument.from_wire(FieldCodec.encode(packet)['document']) == document
    assert any(row.reports_turn(TURN) for row in history)
    assert FieldCodec.decode(OwnerReleaseReceipt, FieldCodec.encode(release)) == release
    assert before == files(service.root)


@pytest.mark.parametrize('entry,arguments,refusal', [
    ('read_original_owner.py', ('ROOT', 'unused', 'not_a_projection'), 'not_a_projection'),
    ('seed_original_owner_capture.py', ('ROOT',), 'Select the current-runtime owner fixture'),
    ('original_owner_capture_installed_pilot.py', (), 'Select a current-runtime attachment'),
])
def test_invalid_read_modes_refuse_before_root_access(
    tmp_path, entry, arguments, refusal,
):
    root, route = tmp_path / 'must-not-be-created', tmp_path / 'route'
    replacements = {'ROOT': str(root), 'ROUTE': str(route)}
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(SOURCE / 'src')}
    result = subprocess.run([sys.executable, '-B', str(SOURCE / 'tools/cutover' / entry),
                             *(replacements.get(value, value) for value in arguments)],
                            input='', env=environment, capture_output=True, text=True)
    assert result.returncode != 0
    assert refusal in result.stderr
    assert 'AttributeError' not in result.stderr
    assert not root.exists() and not route.exists()
