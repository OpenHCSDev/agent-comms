"""Command contexts and import isolation use real stores and real subprocesses."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.order import Commander
from agent_comms.commander.request import RequestVerb
from agent_comms.comms import Comms
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import RequestGoalOrder


def context(root: Path, name: str, text: str) -> Commander:
    comms = Comms(root)
    comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(root), created_at=1.0))
    comms.goals.update_goal(name, SetGoalAction(text=text, goal_id='request', order=RequestGoalOrder()))
    config = CommanderConfig(commander=name, root=root, cli=Path(sys.executable),
        state=root / 'state', board=root / 'board.md', narrative=root / 'narrative.md',
        skill=root / 'skill.md', rules=(), repos=(), worktrees=root / 'wt',
        scratch=root / 'scratch', disk=root)
    return Commander.of(config)


def test_import_with_empty_home_preserves_the_copied_registry(tmp_path):
    original = context(tmp_path / 'original', 'lead', 'Original owner request')
    copied = tmp_path / 'copied'
    shutil.copytree(original.comms.root, copied)
    registry = copied / 'registry.json'
    before = registry.read_bytes()
    home = tmp_path / 'empty-home'
    home.mkdir()
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('AGENT_COMMS_', 'PI_COMMS_', 'PI_NATIVE_'))}
    environment['HOME'] = str(home)
    result = subprocess.run([sys.executable, '-B', '-c', 'import agent_comms.commander.order'],
                            env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert not tuple(home.iterdir())
    assert registry.read_bytes() == before


def test_two_commands_keep_their_constructor_context(tmp_path, capsys):
    first = context(tmp_path / 'first', 'lead', 'First owner request')
    second = context(tmp_path / 'second', 'other-lead', 'Other owner request')
    for commander, expected in ((first, 'First owner request'), (second, 'Other owner request'),
                                (first, 'First owner request')):
        command = RequestVerb.parse(['list'], '', commander)
        assert command.context is commander
        command.apply()
        assert capsys.readouterr().out == f'request {expected}\n'
