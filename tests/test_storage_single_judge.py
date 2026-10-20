"""Real Git cleanup is admitted once; native results carry only actual preflight denials."""
import collections
import json
import shlex
import subprocess
from dataclasses import replace

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.native_tool_call import CodingCall
from agent_comms.pi_payloads import StorageDenialToolDetails
from agent_comms.storage_rules import StorageDenied, StorageRule, UnitStorage
from test_commander_problem_orders import create


def git(seed, *args):
    return subprocess.run(('git', '-C', str(seed), *map(str, args)), check=True,
                          capture_output=True, text=True)


def command(seed, action, path):
    return shlex.join(('git', '-C', str(seed), 'worktree', action, str(path)))


