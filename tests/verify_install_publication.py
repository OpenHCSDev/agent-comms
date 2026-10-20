"""Exercise candidate Stack publication with installed packages and a private HOME.

No runtime installation, environment creation, real harness configuration, or
service restart occurs. The actual installed SessionStart executable is invoked
through the privately published PATH and its written hook declaration.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = Path("/home/ts/code/projects/agent-comms")
sys.path[:0] = [str(MAIN / "src"), str(ROOT), str(ROOT / "tools")]

from agent_comms.commander.config import CommanderConfig
from agent_comms.field_codec import FieldCodec
from install_gate import ExternalRecord
from agent_comms_hooks import ClaudeHookProgram
from agent_comms_hooks.skill import SkillText
from tools.upgrade_bus import Stack


@dataclass(frozen=True)
class InstalledCoreStack(Stack):
    @property
    def python(self) -> Path:
        return MAIN / "stack/.venv/bin/python"

    def environment(self, *paths: Path) -> dict[str, str]:
        return super().environment(MAIN / "src", *paths)


@dataclass(frozen=True)
class WrittenCommand(ExternalRecord):
    command: str


@dataclass(frozen=True)
class WrittenRule(ExternalRecord):
    hooks: tuple[WrittenCommand, ...]


@dataclass(frozen=True)
class WrittenSettings(ExternalRecord):
    hooks: dict[str, tuple[WrittenRule, ...]]
    retained_user_setting: str


@dataclass(frozen=True)
class HookContext:
    hookEventName: str
    additionalContext: str


@dataclass(frozen=True)
class HookResult:
    hookSpecificOutput: HookContext


def main() -> None:
    home = Path("/home/ts/.cache/agent-scratch/pi-install-gate/p275-publication-proof-final")
    home.mkdir(mode=0o700)
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"retained_user_setting": "keep", "hooks": {}}))
    stack = InstalledCoreStack(ROOT, home)
    stack.publish()
    first = settings.read_bytes()
    stack.publish()
    assert settings.read_bytes() == first, "Repeated refresh duplicated or changed configuration"
    written = FieldCodec.decode(WrittenSettings, FieldCodec.parse(first))
    assert written.retained_user_setting == "keep"
    environment = stack.environment(ROOT)
    environment["PATH"] = str(home / ".local/bin") + os.pathsep + os.environ["PATH"]
    for program in ClaudeHookProgram.members():
        name = shlex.split(program.command())[0]
        selected = shutil.which(name, path=environment["PATH"])
        assert selected == str(home / ".local/bin" / name), name
        for event in program.events:
            commands = tuple(
                hook.command for entry in written.hooks.get(event, ()) for hook in entry.hooks
            )
            assert commands.count(program.command()) == 1, (event, program.command(), commands)
        print(f"published/configured: {program.command()} {program.events}")
    invoked = subprocess.run(
        shlex.split(SkillText.command()),
        input=json.dumps(
            {"session_id": "private-publication-proof", "hook_event_name": SkillText.events[0]}
        ),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert invoked.returncode == 0, invoked.stderr
    result = FieldCodec.decode(HookResult, FieldCodec.parse(invoked.stdout))
    assert result.hookSpecificOutput.hookEventName == SkillText.events[0]
    sources = (CommanderConfig.packaged_skill(), *CommanderConfig.packaged_rules_files())
    for source in sources:
        assert source.name in result.hookSpecificOutput.additionalContext
        assert (
            source.read_text(encoding="utf-8", newline="")
            in result.hookSpecificOutput.additionalContext
        )
    print(
        f"SessionStart rc=0; all {len(sources)} packaged files retained; refresh idempotent; user setting preserved"
    )
    print(
        "Only the private HOME was changed; production publication and service restart were not run"
    )


if __name__ == "__main__":
    main()
