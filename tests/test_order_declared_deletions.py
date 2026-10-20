"""Declared module deletions reach the real commander CLI and saved order codec."""

from agent_comms.change_report import FileLine


def test_file_line_scan_keeps_the_absolute_path():
    assert FileLine.scan('/home/ts/a.py:1') == (FileLine('/home/ts/a.py', 1),)


