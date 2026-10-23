"""Every native unit's system prompt carries the indictment, the fleet's rules and the pattern index."""

import pytest

from agent_comms.commander.package import CommanderPackage
from agent_comms.commander.rule_file import CommanderAudience, FleetAudience, RuleFile
from agent_comms.field_codec import MalformedRecord
from agent_comms.native_package import OWNER_INSTRUCTIONS
from agent_comms.slop import Slop
from agent_comms.unit_instructions import UnitInstructions


def test_unit_prompt_carries_fleet_rules_and_never_commander_process(tmp_path):
    text = UnitInstructions.prepare(tmp_path / "UNIT_SYSTEM.md").read_text(encoding="utf-8")
    for source in (OWNER_INSTRUCTIONS, CommanderPackage.indictment(), Slop.catalog() / "README.md"):
        assert source.read_text(encoding="utf-8").strip() in text, source
    rules = RuleFile.packaged()
    fleet = [rule for rule in rules if rule.audience is FleetAudience]
    commander = [rule for rule in rules if rule.audience is CommanderAudience]
    assert fleet and commander and len(fleet) + len(commander) == len(rules)
    assert all(rule.render() in text for rule in fleet)
    assert not any(rule.summary in text for rule in commander)


def test_rule_file_without_an_audience_is_refused(tmp_path):
    source = CommanderPackage.rules_files()[0].read_text(encoding="utf-8")
    path = tmp_path / "undeclared.md"
    path.write_text("".join(line for line in source.splitlines(keepends=True)
                            if not line.startswith("audience:")), encoding="utf-8")
    with pytest.raises(MalformedRecord):
        RuleFile.read(path)
