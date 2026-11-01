"""Every native collaborator carries the same source-derived system instructions."""

from agent_comms.commander.package import CommanderPackage
from agent_comms.commander.rule_file import RuleFile
from agent_comms.native_package import OWNER_INSTRUCTIONS
from agent_comms.slop import Slop
from agent_comms.unit_instructions import UnitInstructions


def test_native_prompt_carries_every_shared_rule_once(tmp_path):
    text = UnitInstructions.prepare(tmp_path / "UNIT_SYSTEM.md").read_text(encoding="utf-8")
    for source in (OWNER_INSTRUCTIONS, CommanderPackage.indictment(), CommanderPackage.voice(),
                   Slop.catalog() / "README.md"):
        assert source.read_text(encoding="utf-8").strip() in text, source
    assert RuleFile.render_packaged() in text
    for rule in RuleFile.packaged():
        assert text.count(rule.render()) == 1, rule.name
