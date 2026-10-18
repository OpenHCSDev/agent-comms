"""Stamp Core's owner protocol identity when building, never when importing."""
from pathlib import Path
import subprocess

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CoreBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        root = Path(self.root)
        relative = "src/agent_comms/_native/core-commit"
        stamp = root / relative
        if (root / ".git").exists():
            commit = subprocess.check_output(
                ("git", "rev-parse", "HEAD"), cwd=root, text=True,
            ).strip()
        elif (commit := "$Format:%H$").startswith("$Format"):
            # A source distribution carries the stamp made by its source build.
            # git archive instead substitutes its own commit above.
            commit = stamp.read_text(encoding="utf-8").strip()
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(commit + "\n", encoding="utf-8")
        build_data["artifacts"].append(relative)
