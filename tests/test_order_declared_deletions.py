"""A cited file line keeps its absolute path."""

from agent_comms.projects.change_report import FileLine
from agent_comms.registry.registry_document import NoTeamProject


def test_file_line_scan_keeps_the_absolute_path():
    assert FileLine.scan('/home/ts/a.py:1', NoTeamProject()) == (FileLine('/home/ts/a.py', 1),)


