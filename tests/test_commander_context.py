"""A command owns its selected bus; importing its declarations opens nothing."""
import os
import subprocess
import sys

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


def test_two_commands_in_one_process_keep_their_explicit_roots(commander_script, tmp_path, capsys):
    first, _ = commander_script
    other = Comms(tmp_path / 'other-root')
    other.registry.declare(other.registry.new_thread('other-lead', frozenset(), str(tmp_path), created_at=3.0))
    other.goals.update_goal('other-lead', SetGoalAction(
        text='The other root has its own owner request', goal_id='other-request', order=RequestGoalOrder()))
    config = first.model_copy(update={'root': other.root, 'commander': 'other-lead'})
    second = Commander.of(config)
    RequestVerb.parse(['list'], '').apply(first)
    assert capsys.readouterr().out == 'request Owner request\n'
    RequestVerb.parse(['list'], '').apply(second)
    assert capsys.readouterr().out == 'other-request The other root has its own owner request\n'
    RequestVerb.parse(['list'], '').apply(first)
    assert capsys.readouterr().out == 'request Owner request\n'
