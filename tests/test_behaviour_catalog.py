"""The behaviour catalog in docs/agents is the family's output, never a hand edit."""

import os
import subprocess
import sys
from pathlib import Path

from agent_comms.behaviour_patterns import BehaviourPattern, PatternCheck
from agent_comms.pattern_sources import PatternSource

ROOT = Path(__file__).resolve().parents[1]


def test_behaviour_catalog_members_name_existing_checks_and_generate_the_file():
    members = BehaviourPattern.members_with(BehaviourPattern)
    assert members
    for member in members:
        assert member.__dict__["signature"].strip(), member
        assert member.__dict__["checks"] and member.__dict__["sources"], member
        assert all(isinstance(check, PatternCheck) and check.cite() for check in member.checks)
        assert all(isinstance(source, PatternSource) and source.cite() for source in member.sources)
    assert (ROOT / "docs/agents/behaviour-catalog.md").read_text() == BehaviourPattern.catalog()


def test_catalog_command_needs_no_commander_installation_or_bus(tmp_path):
    generated = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-m", "agent_comms.behaviour_patterns"],
        cwd=tmp_path,
        env=os.environ | {"HOME": str(tmp_path), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert generated.returncode == 0, generated.stderr
    assert generated.stdout == BehaviourPattern.catalog()
    assert not tuple(tmp_path.iterdir())
