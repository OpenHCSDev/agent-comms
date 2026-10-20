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


