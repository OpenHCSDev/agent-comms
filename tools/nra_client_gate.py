#!/usr/bin/env python3
"""The NRA gate's CI entry for a client repository such as Toad: the checkout it runs in,
measured as AgentCommsProject by NominalRefactorAdvisor's audit. A client ships no
stored-format reader, so it borrows this checkout's; that is the one fact this entry owns
beside which project it measures."""

from __future__ import annotations

from pathlib import Path

from agent_comms_project import AgentCommsProject, CheckoutReader
from nominal_refactor_advisor import audit

if __name__ == "__main__":
    raise SystemExit(audit.gate.main(AgentCommsProject(Path.cwd(), CheckoutReader())))
