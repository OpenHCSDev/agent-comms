"""A command owns its selected bus; importing its declarations opens nothing."""
import os
import shutil
import subprocess
import sys

import pytest

from agent_comms.commander.order import Commander
from agent_comms.commander.request import RequestVerb
from agent_comms.comms import Comms
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import RequestGoalOrder


def test_importing_order_with_empty_home_reads_no_config_and_opens_no_store(tmp_path):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('AGENT_COMMS_', 'PI_COMMS_', 'PI_NATIVE_'))}
    environment['HOME'] = str(tmp_path)
    result = subprocess.run([sys.executable, '-B', '-c', '''
from agent_comms.comms import Comms
from agent_comms.commander.config import CommanderConfig

def forbidden(*args, **kwargs):
    raise AssertionError('import opened a store or read configuration')

Comms.__init__ = forbidden
CommanderConfig.read = classmethod(forbidden)
import agent_comms.commander.order
'''], env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize('deep', (False, True))
def test_context_copy_rebinds_after_root_updates(commander_script, tmp_path, deep):
    first, _ = commander_script
    selected = tmp_path / 'selected'
    copied = first.model_copy(update={'root': selected}, deep=deep)
    assert copied.root == copied.comms.root == selected
    assert copied.comms is not first.comms
    assert first.root == first.comms.root


def test_relocated_config_never_opens_original_and_command_writes_only_copy(commander_script, tmp_path):
    first, _ = commander_script
    selected = tmp_path / 'selected'
    shutil.copytree(first.root, selected)
    before = {path.relative_to(first.root): path.read_bytes()
              for path in first.root.rglob('*') if path.is_file()}
    result = subprocess.run([sys.executable, '-B', '-c', '''
import os, sys
from pathlib import Path
selected, original = map(Path, sys.argv[1:])

def reject_original(event, args):
    if event in ('open', 'os.listdir', 'os.scandir', 'os.mkdir', 'sqlite3.connect'):
        if args and isinstance(args[0], (str, bytes, os.PathLike)):
            if Path(os.fsdecode(args[0])).resolve().is_relative_to(original):
                raise AssertionError(f'opened original bus: {event} {args[0]}')
sys.addaudithook(reject_original)
from agent_comms.commander.order import Commander
from agent_comms.commander.request import RequestVerb
context = Commander.read_root(selected)
assert context.root == context.comms.root == selected
goal = context.comms.registry.require(context.commander).goal.require()
RequestVerb.parse(['complete', goal.id, '--why', 'Selected copy command proof'], '').apply(context)
assert context.comms.registry.require(context.commander).goal.require().progress == 'Selected copy command proof'
''', str(selected), str(first.root)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert before == {path.relative_to(first.root): path.read_bytes()
                      for path in first.root.rglob('*') if path.is_file()}
    assert Comms(selected).registry.require(first.commander).goal.require().progress == 'Selected copy command proof'


def test_two_commands_in_one_process_keep_their_explicit_roots(commander_script, tmp_path, capsys):
    first, _ = commander_script
    other = Comms(tmp_path / 'other-root')
    other.registry.declare(other.registry.new_thread('other-lead', frozenset(), str(tmp_path), created_at=3.0))
    other.goals.update_goal('other-lead', SetGoalAction(
        text='The other root has its own owner request', goal_id='other-request', order=RequestGoalOrder()))
    config = first.model_copy(update={'root': other.root, 'commander': 'other-lead'})
    config.write(tmp_path / 'other-home')
    second = Commander.read_root(other.root)
    RequestVerb.parse(['list'], '').apply(first)
    assert capsys.readouterr().out == 'request Owner request\n'
    RequestVerb.parse(['list'], '').apply(second)
    assert capsys.readouterr().out == 'other-request The other root has its own owner request\n'
    RequestVerb.parse(['list'], '').apply(first)
    assert capsys.readouterr().out == 'request Owner request\n'
