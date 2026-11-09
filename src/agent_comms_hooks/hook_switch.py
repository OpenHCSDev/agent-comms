"""Turn every Claude Code hook off or on: the way out when a gate is what's wrong.

`stop` works with the bus down: the settings change comes first, and a post that fails is reported
without undoing it.
"""

from __future__ import annotations

import subprocess
import sys
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar

from agent_comms.cli_verbs.declared_verb import DeclaredVerb
from agent_comms.installer.harness_install import merge_json
from agent_comms.nominal.field_codec import Argument
from agent_comms_hooks.shell_commands import EmergencyHookStop


@dataclass(frozen=True, kw_only=True)
class HooksVerb(DeclaredVerb, affix="Verb"):
    """One switch of the setting Claude Code reads on every hook."""

    setting: ClassVar[str] = "disableAllHooks"
    post_seconds: ClassVar[int] = 10
    """Under the 15 s foreground cap, so a wedged bus can't kill the stop before it reports."""

    @classmethod
    def tool(cls) -> str:
        return EmergencyHookStop.program.name

    @abstractmethod
    def switched(self, document: dict) -> None:
        """Leave the setting as this verb means it."""

    @abstractmethod
    def report(self) -> str:
        """What happened, as the person running it should read it."""

    def run(self, home: Path) -> str:
        merge_json(home / ".claude/settings.json", self.switched)
        return self.report()


@dataclass(frozen=True, kw_only=True)
class StopVerb(HooksVerb, declared_name=EmergencyHookStop.verb):
    help = "Turn every hook off and say so in #comms"
    sender: Annotated[str, Argument(flags=("--from",), options={"help": "your name on the bus"})]
    why: Annotated[str, Argument(flags=("--why",), options={"help": "what the gate got wrong"})]

    def switched(self, document: dict) -> None:
        document[self.setting] = True

    def report(self) -> str:
        body = (f"Emergency hook stop pulled by {self.sender}: every Claude Code hook on this host "
                f"is off. Why: {self.why}. `{self.tool()} start` turns them back on.")
        try:
            posted = subprocess.run(
                ("agent-comms", "send", "--from", self.sender, "--to", "#comms", "--type", "alert",
                 "--body", body),
                capture_output=True, text=True, timeout=self.post_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            return f"Hooks are off. The #comms post failed, so say it yourself: {error}"
        if posted.returncode:
            return f"Hooks are off. The #comms post failed, so say it yourself: {posted.stderr}"
        return "Hooks are off, and #comms has been told."


@dataclass(frozen=True, kw_only=True)
class StartVerb(HooksVerb, declared_name="start"):
    help = "Turn the hooks back on"

    def switched(self, document: dict) -> None:
        document.pop(self.setting, None)

    def report(self) -> str:
        return "Hooks are on."


def main() -> int:
    try:
        print(HooksVerb.parse(sys.argv[1:]).run(Path.home()))
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
