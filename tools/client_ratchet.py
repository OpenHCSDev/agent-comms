#!/usr/bin/env python3
"""The debt ratchet's CI entry for a client repository such as Toad: the checkout it runs in,
measured as AgentCommsProject by the refactor-audit this agent-comms checkout pins. A client
ships no stored-format reader, so it borrows this checkout's; that is the one fact this entry
owns beside which project it measures."""

from __future__ import annotations

from pathlib import Path

from agent_comms_project import AgentCommsProject, CheckoutReader

from agent_comms.pinned_audit import audit

if __name__ == "__main__":
    raise SystemExit(audit.ratchet.main(AgentCommsProject(Path.cwd(), CheckoutReader())))
