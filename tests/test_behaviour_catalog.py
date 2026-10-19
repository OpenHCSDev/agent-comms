"""The behaviour catalog in docs/agents is the family's output, never a hand edit."""

from pathlib import Path

from agent_comms.behaviour_patterns import BehaviourPattern

ROOT = Path(__file__).resolve().parents[1]


def test_behaviour_catalog_members_name_existing_checks_and_generate_the_file():
    members = BehaviourPattern.members_with(BehaviourPattern)
    assert members
    for member in members:
        assert member.checks and member.sources, member.declared_name
    assert (ROOT / "docs/agents/behaviour-catalog.md").read_text() == BehaviourPattern.catalog()
