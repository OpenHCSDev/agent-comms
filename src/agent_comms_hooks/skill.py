"""Put the commander skill and packaged rules in its session context."""

from __future__ import annotations

import json

from agent_comms.commander.config import CommanderConfig
from agent_comms_hooks import ClaudeHook, ClaudeHookProgram


class SkillText(ClaudeHookProgram):
    events = ("SessionStart",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{main.__module__}:{main.__name__}")


def main() -> None:
    hook = ClaudeHook.read()
    sources = (CommanderConfig.packaged_skill(), *CommanderConfig.packaged_rules_files())
    text = "\n\n".join(
        f"# Commander file: {path.name}\n{path.read_text(encoding='utf-8', newline='')}"
        for path in sources
    )
    try:
        notice = f"Commander: {CommanderConfig.read(CommanderConfig.path()).commander}."
    except (OSError, ValueError):
        notice = "No commander is configured (installed configuration is missing or invalid)."
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": hook.hook_event_name,
                    "additionalContext": f"{notice}\n\n{text}",
                }
            }
        )
    )


if __name__ == "__main__":
    main()
