#!/usr/bin/env python3
"""The NRA gate's CI entry: this checkout, measured as AgentCommsProject by NominalRefactorAdvisor's
audit. Every measure, the command line, the comparison, its JSON report and its exit status are
NRA's; the one fact this entry owns is which project it measures."""

from __future__ import annotations

from pathlib import Path

from agent_comms_project import AgentCommsProject
from nominal_refactor_advisor import audit

if __name__ == "__main__":
    project, arguments = AgentCommsProject.from_arguments(Path.cwd())
    raise SystemExit(audit.gate.main(project, arguments))
