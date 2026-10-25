"""Declared module deletions reach the real commander CLI and saved order codec."""

from agent_comms.change_report import FileLine
from agent_comms.registry_document import NoCommanderProject


def test_file_line_scan_keeps_the_absolute_path():
    assert FileLine.scan('/home/ts/a.py:1', NoCommanderProject()) == (FileLine('/home/ts/a.py', 1),)


