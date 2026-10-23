"""The system prompt every native unit carries, derived once at preparation.

A system prompt survives every compaction, so a unit never drifts off it: the owner's
instructions, the indictment, every rule whose audience is the fleet, and the
refactor-audit pattern index that orders and send-backs cite.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from agent_comms.commander.package import CommanderPackage
from agent_comms.commander.rule_file import RuleFile
from agent_comms.native_package import OWNER_INSTRUCTIONS, UNIT_INSTRUCTIONS
from agent_comms.todos import Slop


class UnitInstructions:
    """Renders the unit system prompt from its owners; a missing source fails here, at preparation."""

    @staticmethod
    def render() -> str:
        sections = (
            OWNER_INSTRUCTIONS.read_text(encoding="utf-8"),
            CommanderPackage.indictment().read_text(encoding="utf-8"),
            "# Rules every unit follows\n\n"
            + "\n\n".join(text for rule in RuleFile.packaged() for text in rule.unit_rules()),
            (Slop.catalog() / "README.md").read_text(encoding="utf-8"),
        )
        return "\n\n".join(section.strip() for section in sections) + "\n"

    @classmethod
    def prepare(cls, path: Path = UNIT_INSTRUCTIONS) -> Path:
        """Publish the rendered prompt atomically, so a launching unit reads a whole file."""
        text = cls.render()
        descriptor, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.chmod(staged, 0o644)
            os.replace(staged, path)
        except BaseException:
            Path(staged).unlink(missing_ok=True)
            raise
        return path
