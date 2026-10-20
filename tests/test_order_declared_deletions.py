"""Declared module deletions reach the real commander CLI and saved order codec."""
from agent_comms.repository_name import RepositoryName
import json
import os
import subprocess
import sys
from pathlib import Path

from agent_comms.change_report import FileLine
from agent_comms.commander.config import Repo
from agent_comms.field_codec import FieldCodec
from agent_comms.todos import ModuleDeletion, NoOrderDeletion
from test_commander_problem_orders import order, order_body, problem


def test_file_line_scan_keeps_the_absolute_path():
    assert FileLine.scan('/home/ts/a.py:1') == (FileLine('/home/ts/a.py', 1),)


