#!/usr/bin/env python3
"""The debt ratchet's CI entry: this checkout, measured as AgentCommsProject by the refactor-audit
it pins. Every measure, the command line, the comparison, its JSON report and its exit status are
the audit's; the one fact this entry owns is which project it measures."""

from __future__ import annotations

from pathlib import Path

from agent_comms_project import AgentCommsProject
from audit.ratchet import main

if __name__ == "__main__":
    raise SystemExit(main(AgentCommsProject(Path.cwd())))
