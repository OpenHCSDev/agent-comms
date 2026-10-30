#!/usr/bin/env python3
"""The debt ratchet's CI entry: this checkout, measured as AgentCommsProject by the refactor-audit
it pins. Every measure, the command line, the comparison, its JSON report and its exit status are
the audit's; the one fact this entry owns is which project it measures, and the project reads
where its own stack clients come from."""

from __future__ import annotations

from pathlib import Path

from agent_comms_project import AgentCommsProject
from agent_comms.installer.commands import InstallCommands
from agent_comms.pinned_audit import audit

if __name__ == "__main__":
    with InstallCommands() as commands:
        project, arguments = AgentCommsProject.from_arguments(Path.cwd(), commands)
        raise SystemExit(audit.ratchet.main(project, arguments))
