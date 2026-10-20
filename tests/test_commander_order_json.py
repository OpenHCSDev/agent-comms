"""JSON orders use the real commander entrypoint and Core's records."""
import json
import os
from pathlib import Path
import subprocess
import sys

from test_commander_problem_orders import order_body


def test_json_problem_and_send_report_all_fields_then_record_the_correction(commander_script):
    commander, repo = commander_script
    (repo / 'count.txt').write_text('0\n')
    path = Path(order_body(repo))
    original = json.loads(path.read_text())
    cli = [str(Path(sys.executable).with_name('agent-comms-commander')),
           '--root', str(commander.comms.root), 'order']
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')}

    def run(*args):
        return subprocess.run([*cli, *args], capture_output=True, text=True,
                              timeout=60, env=env)

    created = run('problem', str(path), '--to', 'worker', '--due', '23:59',
                  '--where', f'{__file__}:1')
    assert created.returncode == 0, created.stderr
    before = commander.comms.goals.todos.get('P1')
    original['problems'] = ['P1']
    original['order']['report'] = 'Please preserve optional_state and maybe_action identifiers.'
    broken = json.loads(json.dumps(original))
    del broken['order']['check']
    broken['order']['owner'] = 7
    broken['order']['report'] = []
    broken['order']['run']['text'] = False
    path.write_text(json.dumps(broken))
    args = ('send', 'worker', '23:59', str(path), '--clean',
            '--replaces', before.messages[-1].message_id)
    refused = run(*args)
    assert refused.returncode != 0
    for name in ('check', 'owner', 'report', 'run.text'):
        assert f'order.{name}:' in refused.stderr
    assert refused.stderr.count('fix:') == 4
    assert commander.comms.goals.todos.get('P1') == before
    path.write_text(json.dumps(original))
    accepted = run(*args)
    assert accepted.returncode == 0, accepted.stderr
    after = commander.comms.goals.todos.get('P1')
    assert after.order.id != before.order.id
    assert after.order.report == original['order']['report']
    assert after.order.check == before.order.check
    assert after.order.id in accepted.stdout


def test_json_revision_check_refuses_before_execution(commander_script, tmp_path):
    commander, repo = commander_script
    path = Path(order_body(repo))
    document = json.loads(path.read_text())
    marker = tmp_path / 'must-not-run'
    document['order']['check']['command'] = f'touch "{marker}"; git checkout main'
    path.write_text(json.dumps(document))
    result = subprocess.run(
        [str(Path(sys.executable).with_name('agent-comms-commander')),
         '--root', str(commander.comms.root), 'order', 'problem', str(path),
         '--to', 'worker', '--due', '23:59', '--where', f'{__file__}:1'],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
    assert result.returncode != 0
    assert 'never checks out' in result.stderr
    assert not marker.exists()
    assert commander.comms.goals.todos.for_commander(commander.incarnation) == ()
